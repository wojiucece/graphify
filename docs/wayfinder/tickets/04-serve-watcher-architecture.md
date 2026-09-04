# serve 内置 watcher 架构

label: wayfinder:grilling
status: closed
assigned: 2026-09-02（本会话）
blocked-by: []

## Question

守护进程 = serve 进程内置 watcher（Q6=A 已定）的实现架构（HITL）：

- 线程模型：watchdog 库 vs 轮询 vs Windows 原生（ReadDirectoryChangesW）；不引入新依赖的边界
- 防抖/降级算法：从 watch.py 已移植的 codegraph watcher 算法（fork 提交 ce98376）复用路径——直接 import 还是抽公共模块
- 触发链：文件变化 → extract 增量（上游 extract cache）→ graph.json 更新 → FTS 缓存重投影 → serve 热重载，单进程内串行化的实现方式
- serve.py 最小挂载点：新 diff 目标控制在多少行内（现补丁已 1677 行）
- watcher 线程与 serve 查询处理的并发/阻塞关系（GIL 下的重投影耗时对查询延迟的影响）

## Resolution

裁决（2026-09-02，三项全按推荐 + 用户三条实现陷阱落档）：

### 裁决表

| 决策点 | 裁决 |
| --- | --- |
| 增量单位 | **(b) extract 增量 + 全量 build**（确定性，万级图 ~1-2s；不迁移上游图级 incremental/shrink-guard） |
| watchdog 依赖 | **(b) 软依赖**：import 失败 → 降级 mtime 轮询（零新硬依赖） |
| 默认开关 | **默认关**：`--watch` / env 显式开启，SessionStart hook 拉起时带开 |

### 架构框架

```
新文件 graphify/serve_watcher.py（~250–350 行，纯自定义零上游触碰）
  watchdog Observer（Windows 自动走 ReadDirectoryChangesW）
  → 防抖聚合（复用 watch.py L29-32 已移植常量：≤2 文件 300ms 快窗 / 常规窗；
     复用 codegraph SCOPED_SYNC_MAX_PENDING=500 分批上限）
  → 失败退避（重试上限 5、指数退避至 30s——watch.py 已移植同款）
  → 批次分类（代码 / 文档 / 忽略）→ pipeline 回调

serve.py 挂载点（diff ≤ 25 行）
  启动参数拉起 watcher 线程 + shutdown 钩子
  pipeline 完成后进程内直通失效 _GraphContextCache（不等 mtime 轮询）

触发链：文件变化 → 防抖 → extract 增量（上游 per-file cache，含删除处理）
        → build → graph.json 落盘（原子写）→ FTS 重投影（05 票接口）
        → 缓存失效原子换图
        （watcher 线程重建期间 serve 查询走旧图，完成后原子换——单进程内自然收益）
```

### 用户三条实现陷阱（必须落进实现）

1. **extract 增量的删除语义**（(b) 方案唯一比 (a) 更易错点）：文件删除/重命名时 watcher 收到 DeletedEvent/MovedEvent，但旧 extract 缓存产物**不会自动消失**——全量 build 会用缓存重建已删除的亡灵节点（(a) 的 shrink-guard 天然处理，(b) 必须手动补）。实现：watcher 维护 pending 删除集；pipeline 执行时从 extraction nodes/edges 剔除 `source_file ∈ 已删除` 的条目 + 失效该文件的 extract cache 条目；MovedEvent 拆为 delete + create 处理。防亡灵兜底：build 前按当前文件系统实况过滤。
2. **降级轮询实现细节**：mtime 轮询用 `os.scandir()` 非递归栈实现（可控排除、显式性）。**实测注记**：与 `os.walk` 性能无差异（2827 文件 median 53.4ms vs 53.2ms——Python 3.5+ 的 walk 在 Windows 内部已走 scandir，"快 3-5x"为旧版本记忆）；5s 间隔下 53ms 扫描占空比 ~1%。**防抖调参参考**：codegraph 无轮询降级先例（其 watcher.ts 是 inotify 耗尽 → 永久停用 auto-sync + actionable message，watcher.ts:669 `degrade()`），Python 侧轮询模式参数自定：5s 轮询间隔、轮询发现变化直接进常规防抖窗（300ms 快窗在 5s 粒度下无意义，跳过）。
3. **graceful shutdown 语义**：atexit / serve shutdown 注册的 `stop()` 必须**阻塞等待当前批次 pipeline 完成**（join + 批次完成信号），不许只发信号——否则进程退出时 graph.json 写到一半损坏，下次启动加载残缺图。双保险：graph.json 落盘走原子写（tmp + rename，上游 atomic-writes 纪律，test_atomic_writes.py 在案）。

### 并发模型说明

watcher 线程内联串行执行 pipeline（防抖窗口天然合并批次）；重建期间 serve 查询线程继续用旧图（`_GraphContextCache` 换图指针原子）；sqlite 写（FTS 重投影，秒级）在 GIL 下的查询延迟影响可控——若实测超阈值，再评估批次内分片。
