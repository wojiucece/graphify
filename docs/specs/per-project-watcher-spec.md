***

label: implemented
source: grilling 会话（Q1-Q15 设计树共识，2026-09-07）；三轮评审修订——自审 I1-I5/P1-P3、二轮（补齐改无条件、锁落点独立模块）、三轮（补齐限定惰性挂载、逐出-重查风暴登记），2026-09-07
date: 2026-09-07
implemented: per-project-watcher 票 01-05 全合并（锁原语提升 → registry 惰性挂载/幂等/信号量 → LRU 联动逐出/上限/自禁用复活 → 挂载补齐/跨进程互斥 → 可观测性/嵌套/停机协议/收尾），2026-09-07
----------------

# Spec — serve 进程内 watcher 的 per-project 化

## Problem Statement

graphify serve 的查询侧已经是 per-project 的：`_GraphContextCache` 维护"1 个 pinned 默认图 + LRU 项目图"，每个工具接受 `project_path` 参数按请求切换。但自动同步侧是**单例**：serve 启动时只挂一个 watcher，只盯默认图的项目根。后果：

1. **多项目服务器的新鲜度缺失**：共享 serve 实例服务多个项目时（TRAE 多根工作区、一个 serve 服务本仓 + 存量子项目），只有默认项目享受保存即重建；其他项目的图查询时加载一次后逐渐陈旧。freshness 信封有标注但无自动消除机制，且标注依赖 rebuild_entry 状态文件的存在——非 hook 面触发的陈旧永远等不到修复者。
2. **手动重建回潮**：非默认项目每次想新鲜数据都得跑显式 rebuild（CLI 或 rebuild_entry），codegraph 退役前"后台守护进程持续保持索引新鲜"的体验在多项目场景丢失。
3. **模型不对称**：查询按项目隔离、监听不隔离，`freshness` 判定报告"陈旧"却没有任何机制去消除它——工具面承诺与运行时能力脱节。

## Solution

把 watcher 做成与查询侧对称的 per-project 模型：`WatcherRegistry`（watcher 注册表）管理一组 `ServeWatcher`，**惰性挂载**（`_select_graph` 首次成功加载某项目图时自动挂该项目的 watcher）+ **LRU 联动逐出**（`_GraphContextCache` 逐出某项目 ctx 时同步停掉它的 watcher）+ **挂载即无条件补齐**（惰性挂载时入队一次全量重建，修复挂载前的历史陈旧；默认项目 eager mount 不补齐——完整兑现"谁在被查询，谁的图就新鲜"）。

单项目用户零行为变化：`--watch`/`GRAPHIFY_WATCH` 单开关语义不变，默认项目仍是启动时 eager mount，关闭时一切 watcher 不挂（现状）。多项目用户获得"谁在被查询，谁的图就保持新鲜"的自动体验。

资源有界：watcher 数量上限独立配置，所有 watcher 的重建管线全局互斥串行（防 N 个全量 build 并发拖垮进程），且与 hook 面 rebuild_entry 的跨进程重建互斥（mkdir 原子锁）。

## User Stories

**自动同步（核心）**

1. As a 多项目 serve 用户, I want 查询项目 B 时 B 的 watcher 自动挂载、挂载即入队一次补齐重建、此后 B 的保存即时重建, so that 不需要为非默认项目手动跑 rebuild，后续响应的 freshness 转为 fresh。
2. As a 多项目 serve 用户, I want 项目 A 的编辑只触发 A 图重建、项目 B 的图不受影响, so that 各项目的事实层互不污染。
3. As a 单项目 serve 用户, I want 开启 --watch 后行为与现状完全一致, so that 升级本特性对我零回归。
4. As a serve 用户, I want watcher 与查询共用同一个开关（--watch / GRAPHIFY_WATCH）, so that 不需要理解第二个开关的心智模型。

**资源与生命周期**

5. As a 长期运行的 serve 管理者, I want 长时间无人查询的项目的 watcher 被自动停掉, so that 线程与文件系统句柄不随历史项目单调增长。
6. As a serve 管理者, I want 用独立环境变量收紧 watcher 数量上限, so that 重资源（活线程 + Observer）可以与轻资源（内存 ctx）分别管控。
7. As a serve 管理者, I want watcher 数量达到上限时停"最近最少使用"的 watcher（而非最早挂载的）, so that 上限逐出与 ctx LRU 语义一致，活跃项目不被误伤。
8. As a serve 用户, I want 某项目 watcher 连续失败自禁用后，下次查询该项目时获得全新 watcher, so that 死亡是软的、重入即复活。
9. As a serve 用户, I want serve 停机时所有 watcher 的 pending 批次都被 flush 落盘, so that 每个项目的事实层文件都完整、不丢事件。

**可观测性**

10. As a serve 用户, I want graph_stats 响应中看到所有被监视项目的列表及各自 backend（watchdog/polling）与状态（active/disabled）, so that 多项目下我知道监听全景。
11. As a serve 用户, I want watcher 挂载/停止/自禁用/挂载补齐重建在 stderr 有明确日志, so that 排障时能还原时间线。

**并发与边界**

12. As a 在父仓内嵌套子项目布局的用户, I want 父 watcher 与子 watcher 各自独立建各自的图, so that 两张图语义各自正确（隐藏目录如 .worktrees 已天然互不可见）。
13. As a 使用 GRAPHIFY_OUT 覆盖输出目录的用户, I want watcher 的 out_dir 推导与查询侧 project_path 解析走同一套逻辑, so that 特殊布局下监听 rebuild 的目标与查询的目标是同一个 graph.json。
14. As a serve 用户, I want 项目 B 的 watcher 重建与同一项目的 SessionEnd/PreCompact hook 重建互斥, so that 两个重建者并发不会写坏 extract cache。

## Implementation Decisions

### 架构形态（grilling Q1-Q5、Q11）

- **方向**：per-project watcher 注册表（惰性挂载 + LRU 联动逐出）。否决了"单 watcher 跟随 _select_graph 切换重指向"——切换即丢上一个项目的新鲜度、快速切换频繁拆装 Observer、active graph 在并发请求下是瞬态概念。
- **`WatcherRegistry` 类落在 serve_watcher.py**（与 ServeWatcher 同文件，watcher 领域概念聚合）；否决 serve.py 闭包式管理（撑爆挂载 diff 预算且不可单测）与独立新文件（过碎）。
- **挂载触发**：`_select_graph` 首次成功加载某项目图时自动挂（自动挂载，否决显式订阅工具——与查询侧"用到即加载"零心智模型对称）。默认图保持启动时 eager mount 不变。
- **幂等挂载**：`_select_graph` 会被同项目反复触发，registry.mount 幂等——该项目已有 alive watcher 则跳过（仅 touch 使用序），dead watcher 则替换为全新实例。
- **单开关不变**：`--watch`/`GRAPHIFY_WATCH` 门控一切 watcher 的存在；关闭时零 watcher（现状语义）。

### 挂载即无条件补齐（grilling Q15，自审二轮 P1 修正，三轮 P1 限定适用范围）

- **语义**：**查询驱动的惰性挂载**首次挂 watcher 时**无条件**入队一次全量重建（不判定陈旧）。**默认项目的 eager mount（启动时挂载）不补齐**——保持 Task 10 现状语义；默认项目即使无人查询也在服务，且其图由 SessionEnd/PreCompact hook 维护，启动即补齐会让单项目用户每次 serve 启动都触发分钟级全量重建，违反 US3 零回归。"冷项目没人查过，多半陈旧"的先验只在惰性挂载成立。
- **无条件而非判定的理由**：已核实"图落后于语料"（改源码未重建）无法用现有指纹体系廉价检测——FTS meta 表指纹对比（graph.json 的 mtime_ns/size）只覆盖"缓存落后于图"这一维度（且后者已有 ensure_fts 惰性重建覆盖）；语料变化不动 graph.json，指纹判 fresh，补齐永不触发。精确判定（manifest hash vs 语料扫描）是秒到分钟级扫描，与"挂载不阻塞查询"矛盾；轻量启发式（max-mtime）有 touch 误报。无条件补齐语义诚实——承认无法廉价判定就全量收敛。
- **成本控制**：per serve 进程 per 项目至多一次（每次挂载周期一次——LRU 逐出后重查视为新挂载周期，再触发一次）；补齐走全局信号量排队，不阻塞查询响应。

### 跨进程互斥（自审 P1 / grilling Q14）

- **问题**：per-project 化后，项目 B 的 watcher（本特性引入）与项目 B 的 SessionEnd/PreCompact hook rebuild_entry（既有）会并发重建同一张图。graph.json 双方原子替换且全量收敛（丢失更新无害），但 **extract cache 并发写会静默损坏**；rebuild_entry 的 mkdir 锁不覆盖 watcher，serve 内信号量只管进程内。
- **方案（b-variant，锁原语提升）**：把 mkdir 原子锁算法（路径消毒 + pid 文件 + 600s stale 接管）实现为 graphify 包内独立小模块（`graphify/rebuild_lock.py`），**rebuild_entry 反向 import 它**——依赖方向合法（scripts → graphify 本就是允许方向），锁算法单一事实源。落点选独立模块而非 serve_watcher.py：rebuild_entry import serve_watcher 会引入"重建入口依赖 serve 侧模块"的怪耦合，还连带 watchdog 软依赖 import。否决算法复制（本仓"三引擎逐出漂移"、`_ensure_fts_retry` 双拷贝的教训：锁的 stale 接管语义微妙，双副本迟早分叉）与"记为已知限制"（extract cache 损坏是静默的）。
- **watcher 拿锁失败行为**：本批 restore 待退避重试（复用 `_restore_batch` 既有机制，事件不丢）；hook 重建结果收敛后，watcher 下一轮指纹命中即跳过实际重建。
- **收敛跳过的精化（票 04/05 实现补精确）**：**跳过只适用于空批次**（纯补齐无并入编辑，无事可丢）；非空批次分两种上下文——运行态 restore 待下轮重建（评审 FB2 封丢失窗口，接受一次冗余重建）、停机态诚实告警 "not flushed"（含自愈路径：下轮惰性挂载补齐/下次编辑/hook 拾取；不静默 restore 也不跑全量管线，保停机延迟有界）。backfill 判定用挂载周期标志而非空批次检测——merged batch（补齐+立即编辑，非空）也标注 rebuilding，freshness 信封诚实。
- **锁参数纪律**：锁名消毒算法禁用 `hash()`（进程随机化）、stale 阈值沿用 600s——这些是提升原语时必须原样保留的语义，测试须钉死跨进程互斥（watcher 重建期间 hook 拿锁失败诚实退出，反之亦然）。

### 生命周期与资源（grilling Q2-Q3、Q7、Q9 + 自审 I1/I2/I4）

- **LRU 联动逐出**：`_GraphContextCache` 构造参数加可选 `on_evict` 回调（默认 None 时零行为变化）。**回调仅挂在 LRU 容量逐出处**（popitem）；`invalidate()` 的 pop 不触发——否则每次 pipeline 完成都会停掉刚干完活的 watcher。
- **锁序约束**：on_evict 回调必须在缓存锁**外**执行（缓存锁内回调 watcher 停止是潜在死锁点——watcher.stop 会 join 线程，而 watcher 线程的 pipeline 完成回调会抢缓存锁）。锁外执行的代价：逐出线程阻塞至多一个 join timeout 等 watcher 收尾，可接受。
- **上限**：独立环境变量 `GRAPHIFY_MAX_WATCHERS`，默认跟随生效的 ctx 上限（`GRAPHIFY_MAX_CONTEXTS` 含 env 覆盖的生效值，默认 8）——保证"每个缓存中的项目都可能有 watcher"是默认不变量；默认值待资源实测校准（watcher 是活线程 + Observer + OS 句柄，与纯内存 ctx 资源模型不同，同源数值是便利性起点非资源论证）。
- **配额模型**：pinned 默认 watcher 不占 `GRAPHIFY_MAX_WATCHERS` 配额（与 ctx 缓存"pinned 默认图不占 GRAPHIFY_MAX_CONTEXTS 配额"的既有模型对称）——否则上限逐出为给新项目腾位时会停掉默认项目的 watcher，违反单项目零回归承诺。
- **逐出序 = 使用序**：registry 条目在每次 `_select_graph` 命中时 touch；上限逐出停"最近最少使用"的 watcher（不是最早挂载的——ctx LRU 顺序与挂载顺序会分叉，按挂载序会误停活跃项目）。dead watcher 优先于 alive 被清（腾位时先扫 dead）。
- **自禁用恢复**：注册表内标记 dead 不重试、无定时复活；LRU 逐出该 ctx 后客户端再查询 → 重挂 → 全新 watcher 从零计数（重入复活）。默认图 pinned 不被逐出，其自禁用行为与现状完全一致（死了直到重启），零回归。
- **停机**：先向全部 watcher 发停止信号，再依序 join（信号量持有者优先），每个沿用现有 join timeout，总预算按信号量排队深度计。修正了"逆挂载序串行 stop"的原设计——final flush 在 watcher 自身线程执行且需抢全局信号量，若默认项目（最早挂载、最可能 mid-build）持有信号量，逆序停机会让其他 watcher 的 flush 与进程退出赛跑，丢 pending 批次（违反铁律 2）。三挂点（stdio finally / http lifespan / atexit）调用形态零改动。

### 并发与隔离（grilling Q4、Q8）

- **全局信号量 = 1**：所有 watcher 的重建管线互斥串行。等闸期间新事件继续并入各自防抖窗（Task 10 已验证的批次聚合语义，不丢事件）。全量 build 是 CPU 密集操作，N 个并发 build 会互相拖慢且内存峰值 ×N。
- **嵌套根允许双 watcher**：父 watcher 把非隐藏子项目文件纳入父图语料（既有行为），子 watcher 建子图——同一次编辑触发两条管线各建各的图。隐藏路径段（.worktrees 等）已被 `_should_track` 排除，天然零冲突；非隐藏嵌套属罕见自找布局，接受成本重叠。否决了父 watcher 排除子项目（静默改变现有用户图内容）与子项目跳过挂载（违背需求初衷）。
- **watcher 间无共享可变状态**：extract cache 按项目根隔离、FTS 缓存按 out_dir 隔离、graph.json 各自原子替换——多实例并发安全的论证在实施时落实为测试。
- **并发首查幂等**：并发请求同时首次查询同一项目时，registry.mount 必须幂等（双检查，只挂一个 watcher）。

### 可观测性（grilling Q6）

- **graph_stats 扩展**：响应加 `watched_projects` 数组（每项 project_root + backend + 状态 active/disabled），**始终存在、空时为 `[]`**（可预测且加性字段不破坏既有消费者）。不加新工具（工具面面积预算约束）。

### 路径解析（约束，非新决策）

- **out_dir 推导必须与查询侧对齐**：票 02 前的 `mount_watcher`（已退役，见 Further Notes 与 Implementation Notes）曾用 `graph_path.parent.parent` 推测 project_root，非标准布局（GRAPHIFY_OUT 覆盖）会推错。per-project 化后 watcher 的 (project_root, out_dir) 必须从 `_resolve_graph_path` 的同一解析链取得，不再反推——该约束仍是绑定设计（registry.mount 的 (project_root, out_dir) 全部由调用方显式传入）。

### serve.py 挂载预算

- 架构票 04 的"serve.py 挂载 diff ≤25 行"约束继续成立：serve 侧改动限于五处触点——注册表构造、_select_graph 挂载点、缓存 on_evict 接线、停机串行化、graph_stats 喂点（逻辑收敛在 registry.status_summary()，serve 侧每处 1-3 行），逻辑全在 serve_watcher.py。

## Testing Decisions

- **只测外部行为**：测试断言"项目 A 编辑后 A 图更新、B 图不变"这类可观察结果，不断言注册表内部数据结构。
- **最高接缝 = 双临时项目 E2E**：tmp 下建 proj-a/proj-b 各带 graphify-out 与最小语料，单进程内挂两个 watcher 走真实管线（真实 extract→build→FTS，无 mock）。主断言四条：
  1. proj-a 编辑（防抖窗操控）后 proj-a 的 graph.json 更新且 proj-b 的 graph.json 字节不变；
  2. LRU 上限设 1 时查询 proj-b → proj-a 的 ctx 被逐出 → proj-a 的 watcher 停止（可经 registry 状态断言）；
  3. 注入连续失败使 proj-a watcher 自禁用 → 逐出 → 重查 proj-a → 新 watcher 挂载且计数从零；
  4. 挂载补齐（无条件语义，仅惰性挂载）：proj-b 的图预先弄陈旧（改语料不重建）→ 查询 proj-b 挂 watcher → 无条件补齐重建后 graph.json 反映新语料；每次挂载周期至多入队一次（同项目逐出重入 = 新周期，再补一次；alive 期间反复查询不重复补齐）。
- **嵌套 E2E**：非隐藏子项目布局夹具（父根 + 子目录各带 graphify-out），断言同一次编辑后父子两图各自更新（各建各图，互不污染）。
- **跨进程互斥测试**：真实 mkdir 锁双持有方竞争——watcher 持锁重建期间，同一项目的 rebuild_entry 拿锁失败诚实退出（exit 3）；反向 hook 持锁期间 watcher 批次 restore 重试。锁提升后 rebuild_entry 既有锁测试必须原样通过（零回归线）。
- **既有接缝复用**：ServeWatcher 构造接缝与 Task 10 测试模式（防抖窗操控、final flush 探针、无半写断言）直接平移到双项目版。
- **新接缝仅一个**：WatcherRegistry 类独立单测（挂/停/上限逐旧按使用序/dead 优先清/幂等挂载/status 汇总），不依赖 serve 闭包环境。
- **回归锁**：单项目 --watch 行为的全套 Task 10 既有测试必须原样通过（零回归验收线）；**默认项目挂载不触发补齐**（eager mount 无重建语义）有显式断言；graph_stats 的 watched_projects 扩展有专门断言（含"零 watcher 时为空数组"）。
- **锁序验证**：on_evict 回调在缓存锁外执行——用"watcher pipeline 完成回调抢缓存锁的同时触发逐出"的并发测试钉死无死锁。
- **Prior art**：tests/test_serve_watcher.py（Task 10 的 E2E 模式）、tests/test_watch_rebuild_trigger.py（退役锁）、tests/test_rebuild_entry.py（mkdir 锁与 stale 接管的既有测试，锁提升后的回归基线）、本仓历轮评审教训（金标门暗置、手工 fixture 掩盖真实管线丢失）均指向验收必须走真实管线。

## Out of Scope

- **watch.py CLI**：一次性命令模式（跑完即退），无 per-project 常驻隔离诉求。
- **多项目 serve 的 project_path 解析一致性审计**：除 out_dir 对齐这一处必要改动外，不做全面审计。
- **watcher 优先级/权重**：所有 watcher 平等竞争全局信号量，无项目优先级。
- **跨 serve 实例协调**：多个 serve 实例监听同一项目的去重不纳入（注：watcher ↔ hook 的跨进程互斥在 scope 内，见 Implementation Decisions——排除的只是"serve↔serve"）。
- **freshness 判定逻辑变更**：现有点查 freshness 语义不动，本特性只是让"陈旧"更少发生。

## Implementation Notes（票 05 实现偏差——全部为精度精化，语义零回退）

- **backfill 判定改挂载周期标志**（非空批次检测）：`_backfill_cycle` 标志由 `_enqueue_backfill` 置位、`_flush_batch` 消费（锁忙早退不消费、重试保留）。merged batch（补齐+立即编辑，非空）也标注 rebuilding——比 spec"空批次=补齐"的字面更诚实，freshness 信封在 watch 面无缺口。
- **停机 join 序落地为 `_holding_gate` 标记**：pipeline 内 gate.acquire/release 两侧 set/clear，`stop_all` 经 `_shutdown_order` best-effort 快照——"信号量持有者优先"从设计描述变为可测实现（断言 join 序）。
- **`stop()` 拆三方法**（`_signal_stop`/`_join_and_finish`/`_stop_observer`）：修复 observer 泄漏——自禁用后早退分支也停/join observer（"线程死了但 observer 可能活着"的统一咽喉）；stop_all 信号/join 两阶段复用同一拆分，Task 10 单 watcher stop() 行为零变化。
- **`mount_watcher` 退役删除**：ticket 02 已切换 registry.mount，serve.py 无生产调用方；`_WATCH_ENV_TRUE`（serve_watcher 内）随之删除。Task 10 三个直调 mount_watcher 的测试更新为 registry/serve 语义（on_complete 回调契约平移）。
- **锁忙 stderr 日志节流**：`logger.debug` 提升为每段锁忙一条 `print`（`_lock_busy_logged` 过渡标记，成功复位）——满足"跨进程锁竞争单行日志"而不被 1s 重试刷屏。
- **`stop_all` 置 `_stopped` 标记**：并发挂载竞态不变式显式化——"被 stop_all 停 或 由 atexit 覆盖"两者必居其一；挂载行为不变。
- **graph_stats 喂点实现**：`_tool_graph_stats` 返回 5 元组 `(text, found, scanned, None, {"watched_projects": registry.status_summary() if registry else []})`，经既有 `_apply_envelope` 的 extra_meta 并入 `_meta`——serve.py 单行（预算 25/25），正文不变、加性字段零破坏既有消费者；watch 关时 `registry is None` → `[]` 恒存在。

## Further Notes

- **与 codegraph 退役的关系**：本特性是 ADR-0001 退役决策的体验补全——退役时承诺的"后台守护进程持续保持索引新鲜"在多项目场景的兑现。独立成票而非塞进退役票，因为退役票已 closed。
- **事件处理粒度**：`_should_track` 的隐藏路径排除（含 .worktrees）是本次嵌套零冲突的关键前提，已核实既有实现（扩展名 + GRAPHIFY_OUT 段 + 隐藏段 + vendored 目录 + graphifyignore 五重过滤）。
- **挂载补齐的窗口语义**：补齐覆盖"挂载时刻已存在的历史陈旧"（仅查询驱动的惰性挂载；默认项目 eager mount 不补齐，见 Implementation Decisions）；挂载后即时编辑由 watcher 正常管线覆盖，两者无缝衔接（补齐重建入队时挂载后事件并入同批次）。触发面性质：每次挂载周期至多一次的**惰性重建触发面**（LRU 逐出重入 = 新周期）——serve 跨会话长驻，与会话周期无关；与 SessionEnd/PreCompact hook 同管线（共用跨进程锁与全量收敛语义）。
- **风险登记**：信号量=1 串行在"多项目同时高频编辑"场景下有吞吐损失（等待期批次聚合缓解）——接受，理由是全量 build 成本远高于等待成本；若未来实测成为瓶颈，升级为按核数的并行度是独立小改动。另：**逐出-重查循环的补齐风暴**——缓存压力大的 serve 上，高频逐出/重查的项目会反复全量重建占用信号量（默认 LRU=8 下逐出罕见，先接受）；若实测出现，加"距上次补齐的节流窗"是独立小改动。
- **风险登记（票 05 补充，全部接受为已知限制）**：
  - **absolute-GRAPHIFY_OUT 自触发风险**（Task 10 遗留）：`_should_track` 的 `_GRAPHIFY_OUT in rel.parts` 检查的是相对路径段——GRAPHIFY_OUT 为绝对覆盖且指向 watch-root 内时永不匹配，out 目录文件会被纳入语料，graph.json 落盘可能自触发重建循环。默认布局（相对覆盖/绝对覆盖在 root 外）不受影响；特殊布局用户自担。
  - **600s stale 窗口无 pid mtime 刷新**（theory-only）：`_acquire_lock` 遇锁时读 pid 文件 mtime 判 age，若持锁方是长驻进程（非短 CLI），pid 文件 mtime 陈旧但锁仍有效——600s 阈值内判忙、超阈值误接管。基线是 hook 面同步重建 65s 级 vs 600s 阈值 = 9× 余量，理论风险实际不触发。
  - **convergence-skip 固有丢失窗口**（窄）：编辑落在 hook 的 extract 读完之后、watcher 1s 级重试之前的窗口内——hook 图不含该编辑、watcher 收敛跳过判定为"已收敛"。宽度为 extract 完成到下次重试之间（秒级），spec:74 的"非空批次 restore/停机告警"裁决 + 停机 not-flushed 告警共同兜底，接受。
  - **`_begin_state`/hook `_state_path` 推导分歧**（GRAPHIFY_OUT 覆盖下）：watcher 的状态文件路径按 out_dir 推导、hook 按自身解析推导，绝对覆盖布局下可能指向不同状态文件——graph_fingerprint 收敛参照精度降级，语义安全（最坏多一次重建）。
  - **逐出-重入竞态（锁外回调）**：on_evict 在缓存锁外执行，逐出线程 stop→join 与并发重查的 mount 存在瞬时窗口——watcher 可能被刚挂的新实例覆盖/或旧实例已逐出新查询重挂，瞬态 watcher 丢失自愈于下一次查询（spec 锁序约束 §"锁外执行的代价"的成本条款延伸，接受）。
- **锁提升的迁移面**：rebuild_entry 的锁函数替换为 import 是机械改动，但其调用方（hooks、CLI）的退出码语义（exit 3）必须在锁提升后逐字保留——退出码空间是既有契约。
