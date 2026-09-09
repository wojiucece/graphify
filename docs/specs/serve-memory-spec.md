# serve 内存优化（serve-memory）

label: ready-for-agent
created: 2026-09-08
spec-source: V4（三轮 grill 裁决 + 用户三项补充发现定稿）→ V4.1 评审修订（P0 /query 阻塞窗口纠正、P1 防抖落点/门控位置/锁 owner 约束、P2 工具化建议）→ V4.2 复审合并（锁 owner 条款去重、扫描上界措辞对齐门控位移、防抖双方案合并为探活+launch-marker、行号统一 117-183、--skip-global 定位统一"随票顺带"）

## Problem Statement

1. **RSS 远超工作集**：graphify serve 长驻进程实测 RSS 341→651MB（一日 2-3 轮重建循环），真实工作集仅 ~180MB（解释器+库 17MB / DiGraph+trigram 48-60MB / communities / HTTP+sqlite+watcher ~25MB）
2. **水位累积（主因）**：graph.json 每次写盘 → 缓存 key (mtime_ns, size) 失效 → 下次查询重载，重载瞬间**旧图+新图共存**（~50MB×2）+ json 解析峰值 + trigram/communities 重建叠加；Windows 堆 freelist 不还 OS，RSS 停在历史峰值附近。写盘触发面三个：CLI update 增量 / precompact→rebuild_entry 全量 / watcher flush。**非泄漏**（trigram 随图释放、逐出路径无引用残留，均已排除）
3. **冗余全量重建**：惰性挂载的无条件补齐在图新鲜时仍触发 60s 级全量重建（CPU + 内存尖峰 + graph.json 重写）
4. **无内存归零机制**：server 长驻跨会话，水位只增不减

**架构事实（约束设计）**：server 启动唯一时机是 sessionstart-graphify-server.sh；**无 MCP 注册**，消费方是 prompt-hook 每条 prompt 一次的短 HTTP `/query`（prompt_hook.py:117-183），失败即 `_query_locally` 进程内回退。

## Solution

三项互补改动：

- **R-E evict-before-reload**：重载前先释放旧图（cache 侧 + 闭包侧双点），重载峰值砍约一个图（~50MB+）
- **R3 idle 自杀 + 自愈闭环**：`--idle-timeout`（默认 3600s）无 prompt 流量即优雅退出（final flush 完整停机协议）；prompt-hook 失败分支非阻塞拉起 server，下一条 prompt 恢复 HTTP——水位按空闲段归零
- **R1 补齐门控**：max-mtime + source_count 双条件，新鲜项目重挂零重建，陈旧（改/增/删）仍修复

## User Stories

**内存**
- US1 查询同一项目一整天，RSS ≤ 250MB（含一次重建重载循环），不随重建轮次爬向 500MB+
- US2 watcher/hook 重建后的下一次查询重载，不再旧图新图同时驻留（峰值砍 ≥ 一个图）
- US3 停止工作 1h 后 server 自动退出释放全部内存；退出前 pending 批次完整 flush（铁律 2）

**自愈**
- US4 server 因空闲退出后：下一条 prompt 立即拿到结果（本地回退），再下一条恢复毫秒级 HTTP

**重建正确性**
- US5 图新鲜的项目重新挂载（idle 复活 / dead-replace）不触发冗余全量重建
- US6 改了语料未重建的项目，重新挂载仍被修复（补齐触发）
- US7 未被监视期间**删除**的文件，重新挂载被捕获（count 维度，无幽灵节点）

**运维**
- US8 不改代码即可回滚：`GRAPHIFY_IDLE_TIMEOUT=0` / `GRAPHIFY_BACKFILL=always` / R-E git revert
- US9 既有语义零变化：默认项目 eager mount 不补齐、--watch 关闭零 import、on_evict 仅容量逐出触发、corrupt graph 期间每查明确报错

## Implementation Decisions

### R-E：evict-before-reload

- **双引用点（缺一不可）**：
  1. **cache 侧**：`_GraphContextCache.load()`（serve.py:149-183）key 失配且 entry 存在 → 先 `entry["G"] = None; entry["communities"] = None` 再 `_load_entry()`，完成后整体替换。**不 pop、不触发 on_evict、保持 LRU 位**（刷新≠容量逐出）；加载本在锁内，无新增竞态
  2. **闭包侧**：`_select_graph` 在 `_load_ctx` 前 `G, communities = None, {}`；**失败恢复旧图**（`except: G, communities = old; raise`）——当次请求服务陈旧但完整的旧图，freshness 信封标注
- **失败路径裁决（grill Q1）**：cache 侧重载失败 → **pop entry**。置空 entry 若原样留存（G=None + 旧 key），下次 stat 同 key 缓存命中返回 (None, None) 崩溃；pop 语义 = "缓存只持有确认新鲜的图"，corrupt 期间每查重试每查报错——与今日行为一致（今日 key 恒失配同样从不服务旧图）
- **/query 阻塞窗口（预期行为，非 bug）**：重建后首次 /query 在 `_select_graph` 处拿锁阻塞 2-4s（锁内 json 解析），完成后返回正常完整结果——HTTP 感知为"这次慢"，**不降级本地回退**。G=None 中间态被锁互斥完全屏蔽：load() 全程持锁（stat→置空→加载→替换原子），get() 同锁（serve.py:193），时序上 load 先于 get → get 必见重载完成后的新 entry。corrupt 场景走 handler 的 except → 500 → prompt-hook 本地回退（serve.py:3557 既有错误路径，非本窗口）。**并发测试断言"阻塞 ≤2-4s 后正常返回"，不是降级——不得为制造 None 可见窗口把置空移出锁外（那才引入真竞态）**
- **归属**：~15 行留在 serve.py——load() 行为修补，与票 03 on_evict 接线同待遇（分层惯例的上游行为修补例外）
- **G 消费者审计**：已核实工具 handler（serve.py:3441）/ resources（:3383）/ /query（:3552）全部先过 `_select_graph`；实施时逐路径复核确认

### R3：idle 自杀 + 自愈闭环

- **模块归属（用户发现 1）**：idle 监视器独立 **`graphify/serve_idle.py`**（ASGI middleware + daemon timer + uvicorn Config/Server 包装，~35 行全在此）；serve.py 触点 ≤5 行（import + 接线）——遵循 fts_cache.py / serve_watcher.py 分层先例
- **参数**：`--idle-timeout` 默认 3600s；`GRAPHIFY_IDLE_TIMEOUT` 覆盖（argparse default 从 env 取，同 `--api-key` 模式）；0 禁用；**仅 http transport**（stdio 随 stdin 退出）
- **活动定义 = 任何 HTTP 请求**（/query + /health 等，ASGI middleware 记 last-activity）。/health 计入续命：本地单用户无外部监控可接受；**注记：未来接入外部监控时 /health 需排除续命**
- **退出路径**：`uvicorn.run` 改 `Config+Server` 持句柄；daemon 线程每 60s 检查，超时置 `should_exit=True` → 优雅退出 → lifespan finally（serve.py:3756-3764）→ stop_all → **final flush 完整停机协议（铁律 2）**；`timeout_graceful_shutdown=30` 安全带
- **ensure-server 自愈闭环**：
  - 新建 `scripts/ensure-graphify-server.sh`（从 sessionstart 抽取 health-check + nohup 启动，单一事实源）；sessionstart 改调共享脚本（启动行为不变）
  - `prompt_hook._query_via_http`（prompt_hook.py:117-183）失败分支：非阻塞调 ensure-server → 本条走 `_query_locally` 回退 → 下一条 prompt 恢复 HTTP
  - **拉起防抖（跨进程，落点 = ensure-server 脚本内）**：prompt_hook 每 prompt 触发新进程（进程内状态不可靠），防抖由 ensure 脚本持久化：先探活（curl /health，通了直接退出）→ 失败后的重复拉起由 launch-marker 条款抑制。保证验收 6 的"ensure-server 恰一次"
  - 闭环语义：server 存活 ⇔ hysteresis 窗口内有 prompt 流过
  - **复活 default 漂移裁决（grill Q3）**：复活者 root 成为 pinned default（接受漂移 + 注记）——prompt-hook 恒传 project_root，default 几乎无消费方
  - **防抖（拉起风暴防护）**：防抖落在 **ensure-server 脚本内**（单一事实源）——启动时写 launch-marker 时间戳（如 /tmp/graphify-serve.launch），marker <30s 新鲜则跳过拉起。覆盖两个竞态：server 启动中端口未开（2-4s 窗口内重复 prompt 重复拉起）与双 prompt 并发复活。理由：prompt_hook 是每 prompt 新进程，进程内时间戳不可持久，脚本侧是唯一落点；bind 冲突自解降级为二道防线注记
  - 票面细节：ensure-server 的 curl 加 `--max-time` 上限（防挂死 server 卡 prompt）
- **设计注记**：mid-build 退出最坏 ~90s 有界（join 60s + graceful 30s）；死窗内编辑由下次编辑的全量重建 / sessionend hook 收敛，非默认项目另有 R1 门控重挂兜底（默认项目 pinned 无补齐，靠前两者）

### R1：补齐门控

- **门控位置（挂载路径零阻塞）**：门控在**补齐批次的 flush 处理内（watcher 线程）执行**，不在 `_enqueue_backfill` 内——mount 仍无条件入队（廉价标志位，查询路径零新增延迟），watcher 线程 flush 纯补齐批次时先跑 `_should_backfill(root, out_dir)`：max(语料 mtime) ≤ graph.json mtime **AND** `extract.collect_files` 扫描计数 == 状态文件记录的 source_count → 跳过 pipeline、清 pending（debug 日志一行）；否则照常执行。理由：同步扫描是秒级，放查询路径会破坏"挂载不阻塞查询"既有不变量；watcher 线程非延迟敏感——per-project-watcher-spec :68"精确判定与不阻塞查询矛盾"的裁决由此消解（矛盾源于把扫描放查询路径，移至 watcher 线程即不复存在）。**mixed batch 不门控**：backfill 与挂载后编辑并入同批次时直接重建（编辑必然使语料变旧、门控也会放行；显式跳过防实现者误将门控套到混合批次吞掉编辑）
- **三类陈旧全覆盖**：修改 = mtime / 新增+删除 = count（**纯 max-mtime 的删除盲区由此封死**——幽灵节点是本仓反复战斗的回归类）
- **数据源裁决（grill Q2，事实锁定）**：graph-derived（G 内 distinct source_file 对比收集数）被事实否决——实测 graph 942 个 source_file ≠ collect 口径（.json ∈ CODE_EXTENSIONS 但 json/空文件不产节点），恒不等恒触发。**state-file int 是唯一可行源**
- **双写路径（grill Q2 修正，正确性必要）**：rebuild_entry 重建完成 + `serve_watcher._run_pipeline` 末尾**各记一次** `source_count`——只写 hook 面的话 watcher 重建后 count 停留旧值 → 门控误判陈旧 → 冗余重建。计数统一调 **`extract.collect_files`**（extract.py:8126，发现规则本体，零新面，依赖方向合法）。**锁 owner 约束（实施红线）**：`_run_pipeline`（serve_watcher.py:740-808）当前不写状态文件（状态文件是 rebuild_entry 职责，schema v2）——新增双写调用 `rebuild_entry._write_state`（rebuild_entry.py:44）时必须仍持 rebuild_lock：该函数读锁 pid 判断 owner（:433"锁没了则拒写"），锁外调用会**静默丢 count**；实施时确认 `_flush_batch` 的锁生命周期（acquire → finally 释放）覆盖到 pipeline 末尾
- **漂移安全方向**：收集规则分叉 → 计数恒不等 → 恒触发 → 退化为无条件（安全侧失败，不静默漏）
- **扫描上界**：>2s 或文件数超限 → 回退无条件——在 watcher 线程语境下是防大仓 flush 停滞的卫生约束（挂载延迟问题已被门控位置条款的位移消解）
- **职责边界**：门控只管 graph-behind-corpus；FTS-behind-graph 由既有 `ensure_fts` 惰性重建覆盖（Task 06 换源建立），不得混淆
- **逃生口**：`GRAPHIFY_BACKFILL=always`
- **spec 回写**：per-project-watcher-spec.md"挂载即无条件补齐"段追加修订注记（决策前提变化 + mtime+count 算法 + 误报容忍论证），并同步修订 :68"精确判定与不阻塞矛盾"的裁决（门控移至 watcher 线程后该矛盾消解）

### 实施顺序

```
R-E ‖ R3（并行，互不依赖）→ R1（收尾）
```

## Testing Decisions

- **最高接缝**：验收 1 直接驱动 `_GraphContextCache.load()` 两次（构造 key 变化）测 tracemalloc 峰值，无需 server 进程插桩
- **prompt_hook 失败分支测试（用户发现 3，新增）**：mock `_query_via_http` 抛连接异常 → 断言 ensure-server 被非阻塞调用**恰好一次** + 该条返回本地回退结果 + 下条恢复 HTTP 的状态语义——prompt_hook.py 现零测试保护，本票起建立回归网
- **测试适配清单（预置，防误诊）**：票 04 新鲜夹具的"挂载即补齐"测试改 `GRAPHIFY_BACKFILL=always` 或注入真实陈旧；`test_lazy_mount_backfills_stale_graph` 与 self-heal E2E **零改动保留**（有真实陈旧，门控正确性活证）
- **验收表**：

| # | 项 | 标准 |
|---|---|---|
| 1 | R-E 峰值 | load() 两次驱动，重载 tracemalloc 峰值砍 ≥ 一个图（~50MB） |
| 2 | R-E 逐出红线 | 重载刷新不触发 on_evict（watcher 仍 alive） |
| 3 | R-E 并发 | 重载窗口内并发 /query 阻塞 ≤2-4s 后正常返回，无 404/500 或空结果（见 R-E"阻塞窗口"条款） |
| 4 | R-E 失败 | corrupt graph：当次恢复旧图可服务；cache pop 无 None-hit；下查自愈 |
| 5 | R3 idle | `--idle-timeout 2` 静默 3s → 退出 + stop_all + final flush 落盘 |
| 6 | R3 自愈 | server 死后 prompt 实测：首条本地回退 + ensure-server 恰一次；次条恢复 HTTP |
| 7 | R1 门控 | 五路：新鲜跳过 / 陈旧执行 / touch 误报执行 / 删除触发（count）/ 超时回退 |
| 8 | R1 适配 | 适配清单落地；stale 测试零改动通过 |
| 9 | 整体内存 | 单项目 ≤250MB；多项目 5 轮换 ≤400MB 且新鲜重挂零重建 |
| 10 | 回归 | test_serve / test_serve_http / test_watcher_registry / test_mount_backfill_lock / test_serve_watcher / test_rebuild_entry 全绿；check-custom 存在性检查全过（口径为存在性全过而非 exit 0——本机 graphify.exe PATH 环境项恒 exit 1，历轮评审有记录）。顺带项（非验收阻塞）：check-custom.sh 加 `--skip-global` 开关恢复 exit 0 自动化守护语义（定位与理由见 Further Notes 工具化条目） |
| 11 | 隔天 | 新会话起始 RSS 与前日无关 |

## Out of Scope

- 字段分层（-36%/图）：有工具消费，逐工具核对回归成本 > 收益
- CSR 紧凑结构：动全部查询读点；4.4× 是 Python 结构性成本
- data 字典削峰：R-E 后剩余峰值结构性；idle 归零兜底
- watcher flush 降频：触发面语义正确，不为内存扭曲行为
- 重建子进程化：推翻 in-process watcher 架构；idle 归零已覆盖痛点
- LRU cap 收紧：`GRAPHIFY_MAX_CONTEXTS` env 为运行时旋钮兜底（V4 评审移出——idle+evict 后单窗口驻留需 8 项目才触发，真实使用 ≤4）

## Further Notes

- **工具化（随票顺带，非验收阻塞项）**：check-custom.sh 增加 `--skip-global` 开关，把 PATH/版本类环境项降级为 warning 不计数，恢复 exit 0 的自动化守护语义——现状 exit 恒非 0，无法区分"仅环境项"与"新增真 ✗"，fail-on-✗ 守护对脚本自动化失效
- **部署**：editable 安装实证生效——手动 kill 重启一次，后续 sessionstart/ensure-server 自愈拉起；Get-Process 探针观察 RSS
- **回滚**：`GRAPHIFY_IDLE_TIMEOUT=0`（关 idle）+ `GRAPHIFY_BACKFILL=always`（恢复无条件补齐）零代码回退；R-E 纯内存优化无行为变化，git revert；高驻留场景兜底旋钮 `GRAPHIFY_MAX_CONTEXTS`
- **根因记录**：水位累积（主因）/ 真实工作集 ~180MB（合理成本）两层定稿
