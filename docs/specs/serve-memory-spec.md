# serve 内存优化（serve-memory）

label: ready-for-agent
created: 2026-09-08
spec-source: V4（三轮 grill + 用户补充定稿）→ V4.1 评审（/query 阻塞窗口、防抖/门控/锁 owner、P2 工具化）→ V4.2 复审合并 → V4.3 R4/R5 增补（管线出进程化、worker 自取锁、心跳杀树、Job Object、SessionEnd 逐出）→ V4.3.1 评审（controller + 用户双审；A-G 首轮标记，最终措辞以 V4.6/V4.7 为准，不重复归属）→ V4.4 review（SessionEnd 非覆盖红线、Job 嵌套降级、mini 语料、锁序红线）→ V4.5 review（scripts 引导 Blocker、Major 1-4、Minor 1-7）→ V4.6 深审（Major 5 pid 判活、Major 6 锁函数来源、Major 7 验收判据、3 条按字面写错）→ V4.7 尾项（锁探测两探、逃生口停看门狗、注册抑制、日志工程、started 快照、失败覆盖 count、DELETE sync def）→ V4.8 用户终审（Major 9 四态判定表 + WinDLL use_last_error、Major 8 验收三处口径对齐、Minor A 纯年龄回退、R1 owner 断言分域）→ V4.9 二轮终审（L113 段落内两套判据勘误、1200→两处、验收 14 同步两探+判活接管、probe_lock 只读契约、_pid_alive 缺失→True 契约、P2 长耗 worker 注记）→ V4.10 票审修订（_begin_state 归属定 watcher（Minor D 括号勘误）、pinned 豁免落注册侧、工具面锁判活/契约/模式限定随票写死为测试断言）

## Problem Statement

1. **RSS 远超工作集**：graphify serve 长驻进程实测 RSS 341→651MB（一日 2-3 轮重建循环），真实工作集仅 ~180MB（解释器+库 17MB / DiGraph+trigram 48-60MB / communities / HTTP+sqlite+watcher ~25MB）
2. **水位累积（主因）**：graph.json 每次写盘 → 缓存 key (mtime_ns, size) 失效 → 下次查询重载，重载瞬间**旧图+新图共存**（~50MB×2）+ json 解析峰值 + trigram/communities 重建叠加；Windows 堆 freelist 不还 OS，RSS 停在历史峰值附近。写盘触发面三个：CLI update 增量 / precompact→rebuild_entry 全量 / watcher flush。**非泄漏**（trigram 随图释放、逐出路径无引用残留，均已排除）
3. **冗余全量重建**：惰性挂载的无条件补齐在图新鲜时仍触发 60s 级全量重建（CPU + 内存尖峰 + graph.json 重写）
4. **无内存归零机制**：server 长驻跨会话，水位只增不减
5. **活跃开发期连续重建残留（V4.3 转正主因）**：watcher 进程内管线（serve_watcher._run_pipeline）每轮 extract+build+to_json 产生 ~100-150MB **异构**临时对象（AST / dict / JSON 字符串三类分配模式交错），Windows 堆 freelist 不还 OS、obmalloc arena 不整空不释放——活跃开发期 watcher 是主重建路径（实测一日三轮 97.6s/72s/119.3s），周而复始 RSS 线性叠加。与解析 churn 的本质差异（实测判别）：解析 churn 分配模式每轮相同、池收敛后全复用（22.6MB 图 ~15 轮收敛 +49MB 平台）；管线 churn 分配模式每轮不同、arena 永不整空——**有界 vs 无界**。三张既有票的真空带：票 01 管"旧图驻留"、票 02 管"闲时驻留"（活跃期 idle 永不触发）、票 03 减"冗余重建次数"——**没有一张管必要重建的 churn 落点**。进程内无解（obmalloc/CRT 堆碎片化 + Windows 无 malloc_trim），根治必须出进程化

**架构事实（约束设计）**：server 启动唯一时机是 sessionstart-graphify-server.sh；**无 MCP 注册**，消费方是 prompt-hook 每条 prompt 一次的短 HTTP `/query`（`prompt_hook._query_via_http`），失败即 `_query_locally` 进程内回退。

## Solution

五项互补改动：

- **R-E evict-before-reload**：重载前先释放旧图（cache 侧 + 闭包侧双点），重载峰值砍约一个图（~50MB+）
- **R3 idle 自杀 + 自愈闭环**：`--idle-timeout`（默认 3600s）无 prompt 流量即优雅退出（final flush 完整停机协议）；prompt-hook 失败分支非阻塞拉起 server，下一条 prompt 恢复 HTTP——水位按空闲段归零
- **R1 补齐门控**：max-mtime + source_count 双条件，新鲜项目重挂零重建，陈旧（改/增/删）仍修复
- **R4 重建出进程化（V4.3）**：watcher flush 改 spawn 短命子进程执行重建管线，churn 随进程消亡 OS 全额回收——活跃开发期连续重建的线性爬升归零（重启式根治，不是降斜率）
- **R5 会话生命周期逐出（V4.3）**：/query 携带 session_id 注册，SessionEnd 钩子显式注销，引用归零的项目逐出 watcher（复用 on_evict 停机协议）；崩溃场景（SessionEnd 未发出）骑 idle 自杀兜底（有界 ≤1h）

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
- US8 不改代码即可回滚：`GRAPHIFY_IDLE_TIMEOUT=0` / `GRAPHIFY_BACKFILL=always` / R-E git revert / **V4.5 起 R4/R5 有专属逃生口 `GRAPHIFY_REBUILD_WORKER=0`（flush 回进程内同步调用）与 `GRAPHIFY_SESSION_EVICT=0`（逐出 no-op），无需 git revert**
- US9 既有语义零变化：默认项目 eager mount 不补齐、--watch 关闭零 import、on_evict 仅容量逐出触发、corrupt graph 期间每查明确报错

**连续重建（V4.3）**
- US10 活跃开发期连续重建（**逐轮异质变更集**，≥20 轮、前 10 轮为稳态建立期不计斜率）后 serve RSS 收敛平台（**后 10 轮 Private Bytes 斜率趋平**，无线性爬升，判据口径与正文一致），不随轮次线性膨胀；worker 退出后 parent RSS 回落（churn 随进程消亡）
- US11 Claude 对话结束（SessionEnd）→ 该项目 watcher 优雅退出释放内存（在飞重建跑完，产物落盘）；MCP-only 会话的误逐出可承受（下次查询惰性重挂 + 门控零重建，≤2s 扫描）

## Implementation Decisions

### R-E：evict-before-reload

- **双引用点（缺一不可）**：
  1. **cache 侧**：`_GraphContextCache.load()` key 失配且 entry 存在 → 先 `entry["G"] = None; entry["communities"] = None` 再 `_load_entry()`，完成后整体替换。**不 pop、不触发 on_evict、保持 LRU 位**（刷新≠容量逐出）；加载本在锁内，无新增竞态
  2. **闭包侧**：`_select_graph` 在 `_load_ctx` 前 `G, communities = None, {}`。**失败 G 停留 None**（不可观测论证：G 消费者——工具 handler / resources / /query——全部在 `_select_graph` 成功后读闭包 G，失败即 500/isError，无路径读 None 中间态；corrupt 期间每查报错，与今日行为一致）。**I1 勘误（2026-09-09 review）**：删除原"失败恢复旧图（`except: G, communities = old; raise`）——当次请求服务陈旧但完整的旧图"——`old = G, communities` 全程强引用旧图，闭包 G 与 cache entry["G"] 是同一对象，`entry["G"]=None` 在闭包路径释放不掉任何东西，生产 /query 峰值削减被完全抵消；且"当次请求服务旧图"不成立（raise 后 500，旧图实际不被服务）
- **失败路径裁决（grill Q1）**：cache 侧重载失败 → **pop entry**。置空 entry 若原样留存（G=None + 旧 key），下次 stat 同 key 缓存命中返回 (None, None) 崩溃；pop 语义 = "缓存只持有确认新鲜的图"，corrupt 期间每查重试每查报错——与今日行为一致（今日 key 恒失配同样从不服务旧图）
- **/query 阻塞窗口（预期行为，非 bug）**：重建后首次 /query 在 `_select_graph` 处拿锁阻塞 2-4s（锁内 json 解析），完成后返回正常完整结果——HTTP 感知为"这次慢"，**不降级本地回退**。G=None 中间态被锁互斥完全屏蔽：load() 全程持锁（stat→置空→加载→替换原子），`_GraphContextCache.get()` 同锁，时序上 load 先于 get → get 必见重载完成后的新 entry。corrupt 场景走 /query handler 的 except → 500 → prompt-hook 本地回退（既有错误路径，非本窗口）。**并发测试断言"阻塞 ≤2-4s 后正常返回"，不是降级——不得为制造 None 可见窗口把置空移出锁外（那才引入真竞态）**。**M4 注记（I1 修复随带）**：默认图 corrupt pop 后，无 project_path 的 /query 走 `_ctx_cache.get()` → 404 "no graph loaded"（非静默服务旧图）——方向更安全（有 project_path 则 `_select_graph` → 500）；自愈后恢复
- **归属**：~15 行留在 serve——load() 行为修补，与票 03 on_evict 接线同待遇（分层惯例的上游行为修补例外）
- **G 消费者审计**：已核实工具 handler / resources / /query 三处 G 消费者全部先过 `_select_graph`；实施时逐路径复核确认

### R3：idle 自杀 + 自愈闭环

- **模块归属（用户发现 1）**：idle 监视器独立 **`graphify/serve_idle.py`**（ASGI middleware + daemon timer + uvicorn Config/Server 包装，~35 行全在此）；serve.py 触点 ≤5 行（import + 接线）——遵循 fts_cache.py / serve_watcher.py 分层先例。**口径勘误（评审）**：触点预算指 idle 逻辑行（import + 接线），`_idle_timeout_default`/argparse/CLI 样板行不计。import 语义：http transport 恒 import serve_idle（stdlib 纯净 os/threading/time，无副作用零成本），idle 启用时才激活监视（激活非 import 门控）；stdio 路径零 import（随 stdin 退出）——"零 import"回归锁口径为 stdio/serve 路径不拉入
- **参数**：`--idle-timeout` 默认 3600s；`GRAPHIFY_IDLE_TIMEOUT` 覆盖（argparse default 从 env 取，同 `--api-key` 模式）；0 禁用；**仅 http transport**（stdio 随 stdin 退出）；`timeout_graceful_shutdown=30` 安全带仅 idle 启用时设置（0 禁用保持与 uvicorn.run 完全等价）
- **活动定义 = 任何 HTTP 请求**（/query + /health 等，ASGI middleware 记 last-activity）。/health 计入续命：本地单用户无外部监控可接受；**注记：未来接入外部监控时 /health 需排除续命**
- **退出路径**：`uvicorn.run` 改 `Config+Server` 持句柄；daemon 线程每 60s 检查，超时置 `should_exit=True` → 优雅退出 → lifespan shutdown finally → stop_all → **final flush 完整停机协议（铁律 2）**；`timeout_graceful_shutdown=30` 安全带
- **ensure-server 自愈闭环**：
  - 新建 `scripts/ensure-graphify-server.sh`（从 sessionstart 抽取 health-check + nohup 启动，单一事实源）；sessionstart 改调共享脚本（启动行为不变）
  - `prompt_hook._query_via_http` 失败分支：非阻塞调 ensure-server → 本条走 `_query_locally` 回退 → 下一条 prompt 恢复 HTTP
  - **拉起防抖（跨进程，落点 = ensure-server 脚本内）**：prompt_hook 每 prompt 触发新进程（进程内状态不可靠），防抖由 ensure 脚本持久化：先探活（curl /health，通了直接退出）→ 失败后的重复拉起由 launch-marker 条款抑制。保证验收 6 的"ensure-server 恰一次"
  - 闭环语义：server 存活 ⇔ hysteresis 窗口内有 prompt 流过
  - **复活 default 漂移裁决（grill Q3）**：复活者 root 成为 pinned default（接受漂移 + 注记）——prompt-hook 恒传 project_root，default 几乎无消费方
  - **防抖（拉起风暴防护）**：防抖落在 **ensure-server 脚本内**（单一事实源）——启动时写 launch-marker 时间戳（如 /tmp/graphify-serve.launch），marker <30s 新鲜则跳过拉起。覆盖两个竞态：server 启动中端口未开（2-4s 窗口内重复 prompt 重复拉起）与双 prompt 并发复活。理由：prompt_hook 是每 prompt 新进程，进程内时间戳不可持久，脚本侧是唯一落点；bind 冲突自解降级为二道防线注记
  - 票面细节：ensure-server 的 curl 加 `--max-time` 上限（防挂死 server 卡 prompt）
- **设计注记**：mid-build 退出最坏 ~90s 有界（join 60s + graceful 30s）；死窗内编辑由下次编辑的全量重建 / sessionend hook 收敛，非默认项目另有 R1 门控重挂兜底（默认项目 pinned 无补齐，靠前两者）

### R1：补齐门控

- **门控位置（挂载路径零阻塞）**：门控在**补齐批次的 flush 处理内（watcher 线程）执行**，不在 `_enqueue_backfill` 内——mount 仍无条件入队（廉价标志位，查询路径零新增延迟），watcher 线程 flush 纯补齐批次时先跑 `_should_backfill(root, out_dir)`：max(语料 mtime) ≤ 捕获参照 **AND** `extract.collect_files` 扫描计数 == 状态文件记录的 source_count → 跳过 pipeline、清 pending（debug 日志一行）；否则照常执行。捕获参照优先取**状态文件 `source_max_mtime`**（重建/管线在 extract 之前与 count 同一次 `collect_files` 扫描记录的语料 max mtime，stat-to-stat 比较）；旧状态文件（无该字段）回退 `min(graph.json mtime, 状态文件 started)`——两个参照的来历与取舍见 I3 勘误 / I3-勘误-2。理由：同步扫描是秒级，放查询路径会破坏"挂载不阻塞查询"既有不变量；watcher 线程非延迟敏感——per-project-watcher-spec 的"精确判定与不阻塞查询矛盾"裁决由此消解（矛盾源于把扫描放查询路径，移至 watcher 线程即不复存在）。**mixed batch 不门控**：backfill 与挂载后编辑并入同批次时直接重建（编辑必然使语料变旧、门控也会放行；显式跳过防实现者误将门控套到混合批次吞掉编辑）。**【I3 勘误（2026-09-09 实施）】**：mtime 侧比较基准从字面 graph.json mtime 修为 `captured_at = min(graph.json mtime, 状态文件 started)`——纯 graph_mtime 有 mid-rebuild-edit 盲区（编辑落在 extract 与落盘之间时 graph 落盘晚于编辑，判"新鲜"漏修；FB2 停机收敛自愈场景暴露）。started 是 extract 捕获起点，min 取更早者 → 该场景 max(语料 mtime) > captured_at 判陈旧（安全侧）。**【I3-勘误-2（2026-09-09 收尾）】**：mtime 侧比较基准再修为**状态文件 `source_max_mtime`**（重建/管线**在 extract 之前**与 count 同一次 `collect_files` 扫描记录的语料 max mtime，stat-to-stat 比较），旧状态文件（无该字段）回退 `captured_at`。两个理由：(1) 纯 `started` 墙钟参照有 Windows 时钟量化误报——`time.time()` 与文件 mtime 非同一时钟源（float64 在 epoch 量级的 ULP ≈ 238ns），新鲜项目文件写后立即重建时语料 mtime 偶发超前 started ~238ns，误判陈旧触发冗余重建（`test_gate_fresh_mount_skips_rebuild` 组合跑偶发 pipeline=1 的 flake 根因）；stat-to-stat 下未变文件 mtime 两次读取严格相等，无一侧时钟误差。(2) 快照**必须早于 extract**（图内容捕获点）——mid-rebuild 编辑（FB2 停机收敛）落在 extract 之后时，若在 extract 后扫描会把编辑 mtime 记入参照 → 门控误判新鲜吞掉丢失编辑；pre-extract 预取值使该编辑 max_mtime 前进超出参照 → 判陈旧（安全侧）。**【I1（reviewer 实测，2026-09-09 fix round 1）】**：`source_max_mtime` 记录时钳制 `min(max_mtime, time.time() + _MTIME_CLAMP_TOLERANCE_S)`（容差 1s）——未来时间戳文件（touch +2s / NTP 回拨 / os.utime）把参照污染到将来时，被监视期间的真实编辑（mtime < 未来值）在重挂被门控判新鲜**静默吞掉**（漏修，spec 明文容忍的 touch 误报本应落安全方向=冗余重建，未来参照把它翻成不安全方向）；钳制使参照恒 ≤ now+1s，未来戳语料 max > 参照 → 判陈旧退化安全侧（冗余重建，不吞真实编辑）。容差 1s 两头安全：>> 时钟源量化 ~238ns（合法新鲜文件不误剪，I3 flake 不回归）；<< touch/NTP 级未来戳（秒级，恒被剪）。剪后自愈：墙钟越过未来值后下轮重建拿干净参照，粘性打破。回退路径 `captured_at` 是过去值天然安全，无需钳。**【Minor 4（用户终审）】**："剪后自愈"的最坏代价注记：未来戳粘性期内（墙钟未越过未来值前）每次重挂都全量重建（冗余但安全，不吞编辑）；墙钟越过未来值后下轮重建拿干净参照，粘性打破。**【Minor 2（用户终审）】**：门控扫描超时的 daemon 线程在 join 超时后随进程自然消亡（不阻塞 watcher 主循环）；未来优化方向：按 (root, 目录 mtime) 短 TTL 缓存扫描结果，避免每次挂载重复整树扫描（当前未实现，仅记录）。
- **三类陈旧全覆盖**：修改 = mtime / 新增+删除 = count（**纯 max-mtime 的删除盲区由此封死**——幽灵节点是本仓反复战斗的回归类）
- **数据源裁决（grill Q2，事实锁定）**：graph-derived（G 内 distinct source_file 对比收集数）被事实否决——实测 graph 942 个 source_file ≠ collect 口径（.json ∈ CODE_EXTENSIONS 但 json/空文件不产节点），恒不等恒触发。**state-file int 是唯一可行源**
- **双写路径（grill Q2 修正，正确性必要）**：rebuild_entry 重建完成 + watcher 引擎（`_run_pipeline`）末尾**各记一次** `source_count`（I3-勘误-2 后同记 `source_max_mtime`，门控 stat-to-stat 参照）——只写 hook 面的话 watcher 重建后 count 停留旧值 → 门控误判陈旧 → 冗余重建。计数统一调 **`extract.collect_files`**（发现规则本体，零新面，依赖方向合法）。**锁 owner 约束（实施红线，V4.5 出进程化迁移）**：状态写入（`rebuild_entry._write_state` / `_write_source_count`）必须由**持锁进程**执行（函数读锁 pid 判 owner，"锁没了则拒写"，锁外调用**静默丢 count**）。R4 前 owner 是 `_flush_batch` 的锁上下文；**R4 后该责任随 `_run_pipeline` 平移进 worker——双写 second 侧 owner 变为 worker pid（worker 自取锁）**，实施时**显式点名验证**：worker 内 `_write_source_count` 恰在持 lock（owner==worker pid）期间调用、`_flush_batch` 不再持有该锁（否则门控静默退化为无条件——hooks 侧若偶发锁外写被拒，count 永远旧值，每次挂载全量重建）；验证方式 = worker 单测断言锁 owner 匹配 + 锁外调用路径不存在。**【模式限定（V4.8）】"`_flush_batch` 不再持有该锁（锁外调用路径不存在）"只限 worker 模式（R4 默认路径）**——逃生口 `GRAPHIFY_REBUILD_WORKER=0` 下进程内管线回到 `_flush_batch` 持锁（R4 前语义），属正确行为。断言必须按模式分域：worker 模式断言锁 owner==worker pid 且锁外调用路径不存在；逃生口模式断言锁 owner==watcher（R4 前持锁写）——**禁止把"锁外调用路径不存在"写成全局断言**，否则逃生口测试误判回归。**【参照捕捉时刻红线（Minor review，V4.6 按字面写就会错 #2）】**：`source_max_mtime` 的**捕捉时刻必须固定在管线起点（extract 之前）**——worker 内实现 `_write_source_count` 时若把参照记录时间（I3-勘误-2 的 captured_at/started 语意）**退化为管线终点**（如 worker 收尾时才打时间戳或直接用完成时刻），则参照捕捉点跑到图内容捕获点**之后** → mid-rebuild 编辑（mtime 介于 extract 起点与管线终点之间）被判"新鲜" → **重开 I3-勘误封掉的 mid-rebuild 吞编辑盲区**（这正是 I3 两轮勘误的核心教训：参照必须早于图内容捕获点）。实施红线：快照（语料 max mtime + 捕捉时刻）在 worker **取锁后、extract 之前**一次性预取并随批次携带，`_write_source_count` 只落盘这批预取值，禁止在管线终点回溯或重取时间。**【E-1 强化（Minor E-1 review，V4.6）】**：worker 内 `_write_source_count` 若沿用 `self._state_started or time.time()`（serve_watcher :853）且拿不到 watcher 的 `_state_started` → **退化为管线终点 time.time()** → 旧状态文件回退路径 `captured_at = min(graph_mtime, started)` 下 mid-rebuild 编辑 mtime < captured_at → 判新鲜吞编辑（I3-勘误专治盲区重开）。解法（裁决）：**payload 显式带 started**（watcher 入队时点记录，随 stdin JSON 传）或 **worker 在 extract 前自取**并随批次携带——两者皆可，禁止 `time.time()` 惰性取值。**【E-2 失败载荷覆盖 count（Minor E-2 review，V4.6 新增）】**：worker 失败时 `_end_state(error=True)` 直接 `write_text` 覆盖状态文件（serve_watcher :999，不受 owner 检查）→ 下轮门控读不到 source_count 退化为无条件重建。裁决：**显式接受并注记**（安全侧冗余重建——失败后本应重试，无条件退化方向安全；不为失败路径回传已取得快照增加复杂度，保持"失败 = 全量重建"的简单语义）
- **漂移安全方向**：收集规则分叉 → 计数恒不等 → 恒触发 → 退化为无条件（安全侧失败，不静默漏）
- **扫描上界**：>2s 或文件数超限 → 回退无条件——在 watcher 线程语境下是防大仓 flush 停滞的卫生约束（挂载延迟问题已被门控位置条款的位移消解）
- **职责边界**：门控只管 graph-behind-corpus；FTS-behind-graph 由既有 `ensure_fts` 惰性重建覆盖（Task 06 换源建立），不得混淆
- **逃生口**：`GRAPHIFY_BACKFILL=always`
- **spec 回写**：per-project-watcher-spec.md"挂载即无条件补齐"段追加修订注记（决策前提变化 + mtime+count 算法 + 误报容忍论证），并同步修订其"精确判定与不阻塞矛盾"的裁决（门控移至 watcher 线程后该矛盾消解）

### R4：重建出进程化（V4.3）

- **动机与裁决**：Problem Statement 第 5 条转正——活跃开发期 watcher 进程内管线 churn 无界线性叠加，进程内无解（obmalloc arena 不整空不释放 / Windows 无 malloc_trim / CRT LFH 极少 decommit）。候选裁决：A 出进程化（**采纳**，churn 随进程消亡 OS 全额回收）/ B 计数自重启（治标丢热图，A 落地后冗余）/ C 流式化 build/to_json（动上游核心、高冲突面、只降斜率）/ D WorkingSet trim（假解决：只修工作集不修 commit）——**R4 是唯一根治**
- **管线平移（Q1 裁决）**：`_run_pipeline` 主体 + 删除语义 helpers（`_filter_deleted` / `_prune_seed_for_deleted` / `_invalidate_extract_cache`）原样平移进新模块 `graphify/rebuild_worker.py`；以 `sys.executable -m graphify.rebuild_worker` 启动（**必须与 serve 同解释器**——editable install 下 graphify 模块才可见；引用 serve 的 `sys.executable` 而非 PATH python）。**三引擎计数不变**：worker 就是原 watcher 引擎出进程，逐出/门控/删除语义逐字匹配（铁律）——serve_watcher 保留调度层（事件聚合/防抖/门控/LRU/停机协议），管线执行委托。rebuild_entry（hook 面）零触碰。**【Blocker 勘误（review 实测，V4.5）】原"`-m` 启动天然在包内不需要 scripts 引导"错误**：`rebuild_entry` 位于 `scripts/`，**不在** editable install 的包列表（pyproject packages = ["graphify", "graphify.extractors", "graphify.exporters"]），worker 进程内 `import rebuild_entry`（平移后 ≥5 处）启动即 ModuleNotFoundError；serve_watcher 的 `_SCRIPTS_DIR` 引导是模块级副作用、每进程独立、spawn 不继承——**若因 worker import serve_watcher 而"侥幸能用"属隐式副作用，必须显式化**。修法（裁决）：抽共享引导 `graphify/_scripts_path.py`（模块级 snippet：`_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"` 入 sys.path，serve_watcher 与 rebuild_worker 双侧 import 同一 bootstrap，消除双份漂移）；rebuild_worker 显式 `from graphify import _scripts_path  # noqa: F401 副作用：scripts/ 入 sys.path`，不依赖任何宿主模块的隐式副作用。serve_watcher :71-74 内联引导替换为对共享 bootstrap 的调用
- **停机 final flush 通道（既有铁律不回归）**：停机 final flush（stop_all → flush → join）**仍走同一 worker 通道**（spawn + 等 exit），等退出有界——worker 心跳杀树（60s）+ `taskkill /T` 兜底，保证"退出靠 SIGHUP 自杀 + final flush 完整停机协议（铁律 2）"在出进程化下语义不变
- **payload（Q2 裁决）**：stdin 单行 JSON `{root, out_dir, changed[], deleted[], semantic_refresh[], backfill}`（ensure-server 同款 stdin JSON 先例）；argv 只留确定性开关。免疫 Windows argv 32k 限制（大删除批次如 git clean 可数百文件）
- **worker 进程 I/O 裁决（Major 1 review，V4.5 新增）**：worker stdout/stderr **禁用 `subprocess.PIPE` 不 drain**——管线输出量大，PIPE 不 drain 时 Windows 管道 64KB 缓冲填满 → 子进程阻塞在 write → 心跳独立线程照常 touch、age 恒 <60s 不判 hang → worker 永久卡死且 watcher 持全局闸 → 所有项目重建停摆（心跳哨兵对此死锁盲区）。裁决：**stdout/stderr 重定向到磁盘日志文件**（`stderr=STDOUT`、`stdout=open(<out>/.rebuild-worker.log, "ab")` 追加）——天然 drain 无死锁、可诊断；文件直接复用 worker 心跳同目录，正常完成批次开头写分隔线（诊断可用、无轮转负担、批次由头尾标记切分）。**【日志工程细节（Minor D review，V4.6 新增）】**：① 日志在**父进程 `open("ab")` 时点早于 worker 内任何 `mkdir`**（`out_dir` 可能由该轮管线首建；**`_begin_state` 归属（V4.10 定案）：由 watcher 在 spawn 前执行**——直接 write_text 不受 owner 检查，phase=rebuilding 先行立档；worker 只写 complete（持锁期间 `_end_state`+`_write_source_count`），两处不互相覆盖）——**spawn 前父进程先 `out_dir.mkdir(parents=True, exist_ok=True)`**，防 FileNotFoundError 崩 flush；② 日志**只追加无轮转**会在长驻 serve 下无界膨胀——打开时 `if size > 5MB: truncate`（一行成本；批次头尾分隔线保证截断后仍可读）
- **锁让渡（Q3 裁决）**：**worker 自取锁**。**【锁函数来源（Major 6 review，V4.6）】**：直接 `from graphify.rebuild_lock import _acquire_lock`——锁函数本就定义在**包内模块** `graphify/rebuild_lock.py`（近内存迁移独立出来的共享模块），`rebuild_entry` 只是反向 import 它；worker **不经 scripts/ 面转手**（经 scripts/ 面就是 Blocker 同型病）。锁忙 exit 3 → watcher 映射既有 `_LOCK_BUSY` 路径（restore + 退避 + `_converged_while_waiting` 指纹跳过全保留）。watcher **不持锁 spawn**——pid 所有权硬约束（`_write_state` 读锁 pid 判 owner，"锁没了则拒写"，watcher 持锁 worker 写状态文件会被拒）。全局 gate 仍由 watcher 持有等待期间，跨 watcher 串行不变。**【exit 码对齐（Minor 3 review，V4.5）】**：worker 内判锁忙的退出码**引用 `rebuild_entry.EXIT_LOCK` 常量**（=3，[rebuild_entry.py:30](scripts/rebuild_entry.py) `EXIT_OK, EXIT_LOCK, EXIT_SYNC_FAIL = 0, 3, 4`），不硬编码魔法数；watcher 侧映射既有 `_LOCK_BUSY` 时同样比较常量。**【spawn 前探测锁（Major 2 review，V4.5；Minor A 落点修正，V4.6）】**：spawn 是开销大动作（每次起新 Python 进程），锁忙窗口（hook 重建 72-120s）下退避 1s ⇒ 70-120 次无效 spawn + 每次启动即 exit 3（`_converged_while_waiting` 是 spawn **之后**的检查，防不了风暴）。裁决：**两探落点**——① **闸前探一次**（watcher 在进入全局 gate **之前** `probe_lock`，锁忙的项目**根本不占全局闸**，与 Major 1 闸前门控同构——避免占着闸干等 1s 退避，阻塞其他项目跨 watcher 串行）；② **持闸后 spawn 前再探一次**（<1ms，防等待 gate 期间被 hook 抢锁）。两探共用 `probe_lock`（复用 Major 5 的 `_pid_alive` stale 判定）；探测 ② 与 spawn 之间仍存在窄竞态，exit 3 路径保留作后盾。**【probe_lock 只读契约（V4.9 写死）】**：`probe_lock` 是**纯报告**——只返回锁当前是否忙（内部复用 `_pid_alive` 判活/stale 判定），**绝不实施删锁或接管**；锁的移除与接管只发生在持锁路径 `_acquire_lock` 内部（超龄或判死时）。探测侧任何"顺手删锁"都会误删仍活/未超龄的锁（两探是纯读序列，与 spawn 之间的窄竞态由 exit 3 后盾兜底，探测自身不写锁目录）。**【pid 判活与锁探测落地（Major 5 修案 V4.6；Major 9 判定表勘误 V4.8 最要紧）】**：探测的"pid 判活"是**全新机制**——既有 `_acquire_lock` 的 stale 判定是**纯年龄**（`age = time.time() - pid 文件 mtime`，`age > _LOCK_STALE_S` 接管，从不看 pid 死活；`_LOCK_STALE_S = 600`，V4.5 写的"~300s"是误值，勘误为 600）；且 Windows `os.kill(pid, 0)` 三态有雷（不存在 → WinError 87；无权限进程 → PermissionError（WinError 5），若 `except OSError` 判死会**删掉别人正在持有的活锁** → 两个重建并发互踩 extract cache；刚退出句柄未闭 → 假活返回 OK）。裁决：**在包内 `graphify/rebuild_lock.py` 增 `_pid_alive(pid)` + `probe_lock(root)`** + `_acquire_lock` 接管条件升级 `age > _LOCK_STALE_S or not _pid_alive(pid)`；**一份实现、watcher 探测与 hook 取锁共用**——禁止 serve_watcher 内重写任何 stale/pid 判定（Blocker 同型病）；顺带把 hook 面"进程死了但锁没超龄"的 600s 盲窗一并修掉。**【Major 9 判定表（V4.8，实测项目解释器 3.12）】**：V4.6 写的"无权限（OpenProcess 失败即隐含判活，不误删）"把两种失败合并了——"失败即判活"（`if not h: return True`）下 **pid 不存在也被判活** → taskkill 后锁仍等满 600s → Major 4+Major 5 收益归零且留"看起来修了"假象；"失败即判死"（`if not h: return False`）又误删无权限活锁 → 重启 os.kill 那颗 PermissionError 雷。**四态判定表（唯一正解）**：

```
OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION=0x1000, pid) ：
  OK + GetExitCodeProcess == STILL_ACTIVE(259)  → 活
  OK + 退出码 != 259                              → 死（存在但已退出）
  FAIL + GetLastError == 87 (ERROR_INVALID_PARAMETER) → 死（pid 不存在——taskkill 后主场景）
  FAIL + GetLastError == 5  (ERROR_ACCESS_DENIED)    → 保守判活（System/他用户活进程）
  其他 FAIL                                        → 保守判活（安全侧：宁可多等不多删）
```

**实现陷阱（实测）**：`ctypes.windll.kernel32` 下 `ctypes.get_last_error()` **恒返回 0**（87/5 永远匹配不上，两种错法静默走错边）——**必须 `ctypes.WinDLL("kernel32", use_last_error=True)`** 并正确声明 argtypes/restype（stdcall 约定），再 `ctypes.get_last_error()` 取错码。**【Minor A（V4.8，按字面写就会错 #4）】**：pid 文件**缺失或不可解析**时不得按"无 owner 可接管"处理——`mkdir` 与 `write pid` 之间是非原子窗口，读到的活锁恰无 pid 文件；缺失/解析失败 → **回退纯年龄判定**（仅 `age > _LOCK_STALE_S` 才接管），防误接管正在建立中的活锁。**【_pid_alive 返回值契约（V4.9 写死）】**：pid **缺失或不可解析**时 `_pid_alive` 必须**返回 True（保守判活）**——与 Minor A"回退纯年龄判定"是**同一结论的两种表述**（判活 ⇒ 接管条件 `age > stale or not alive` 只剩年龄分支 ⇒ 恰好回退纯年龄）；**禁止实现为 `_pid_alive(None) → False`**（会把刚封死的回退又绕回去——同一颗雷的第三种写法）。**【异常退出的锁释放责任（Major 4 review，V4.5 修订）】**：worker 被 taskkill 后锁残留——现由 `_acquire_lock` 升级后的接管条件（pid 已死即回收，不等到 600s 年龄）兜底，探测时点同一逻辑（`probe_lock` 复用）；正常退出由 rebuild_entry finally 释放，watcher 不额外干预
- **心跳与 hang（Q4e 裁决）**：worker 管线内 daemon 线程每 10s touch `<out>/.rebuild-heartbeat`（mtime 即心跳，无内容解析）；watcher wait 循环每 5s 检心跳 age **> 60s** 判 hang → `taskkill /T /PID` 杀树 → 按失败退避（restore 批次，与今日 pipeline 异常同路径）。60s ≫ 10s 间隔（容 6 次丢失）≪ stale 接管窗口（600s，卡死检测提前一个数量级，且**杀卡死者**而非只让别人重建；V4.6 勘误：两处"~300s"为误值，实际 `_LOCK_STALE_S = 600`）。watchdog 后端覆盖面核查：心跳在 worker 内（管线长跑的主执行体），watchdog 只挂 watcher 调度线程（短sleep循环），两处不重叠。**【心跳基线（Minor 1 review，V4.5 新增）】**：judge hang 的 age 基准**按该次 spawn 时刻重置**（age = now - max(spawn_ts, 心跳文件 mtime)）——防止上批被强杀残留的旧心跳文件让新 worker 一启动就被误杀；等价做法为 spawn 前删除旧心跳文件，spec 取"max(spawn_ts, mtime)"避免竞态（贴近 spawn 瞬间新 worker 首次 touch 前判定窗口）
- **孤儿治理（Q5 裁决）**：serve 启动建 Windows Job Object（`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`），worker 挂入（AssignProcessToJobObject）——serve 无论怎么死（崩溃/被 kill -9），OS 内核杀 worker，零轮询零泄漏。与 R5 会话逐出互补（那是优雅停机，这是崩溃兜底）。**【实现手段（review 发现，未定→补明确裁决）】**：ctypes 封装 kernel32（`CreateJobObjectW` + `SetInformationJobObject`(JobObjectExtendedLimitInformation, LimitFlags=JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE) + `AssignProcessToJobObject` + 需持有 job 句柄勿使 JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE 失效）；**嵌套 Job 权限限制**：serve 若已被外部进程置入某 Job（向量：被 IDE/启动器 Job 托管），`AssignProcessToJobObject` 直接失败 → **失败降级**（记录一次 warning，跳过 Job 挂载——心跳哨兵（60s hang 检测）+ 停机路径显式 terminate 已双兜底，无非受控孤儿窗口）；Job 挂载语义不可静默吞。库依赖：stdlib ctypes 零依赖（本仓不开新依赖的先例），不引入 pywin32。**非 Windows 兜底注记**（诚实，未实现）：Job Object 是 Windows 专属；其他平台无法靠内核联动，回退方案 = watcher 停机路径在正常 join 超时后显式 terminate 子进程（POSIX `Popen.terminate`）+ 心跳哨兵——当前项目 Windows-only，此分支仅注记不实施
- **时延与并发**：~0.3s worker 启动代价 vs 管线 72-120s，噪声；增量 extract cache 跨进程共享（rebuild_lock 协调）不变——增量语义不丢。**【缓存共享半口径（Minor 2 review，V4.5）】**：跨进程共享**仅指磁盘态**（per-file AST 增量缓存 `root/graphify-out/cache`）✓；进程内态缓存（`_WORKSPACE_PACKAGE_CACHE` 等解析/语义缓存）每轮 worker 冷启动重建——属每轮固定启动代价（无正确性影响，含在 ~0.3s 启动噪声量级内），不构成增量语义损失，注记即可
- **残余下界（诚实口径）**：serve 每轮仍有一次 graph.json 解析 churn，实测**有界**（22.6MB 图 ~15 轮收敛 +49MB 平台，同构分配复用旧 arena）——R4 目标是**消除无界线性爬升**，RSS 收敛走平，非绝对零
- **验收口径升级**：RSS 验收从"绝对值"改为"**收敛性**"——逐轮异质变更集连续 N≥20 轮、判据 = **后 10 轮斜率趋平（前 10 轮稳态建立期不计斜率）+ Private Bytes 口径**，替代单点阈值（**V4.9 勘误：本句原残留"增量 ≤0.5MB/轮"是无判别力旧口径，与下文 Major 7 修正自相矛盾——同一段两套判据，已删除并以下文为准**）。**【判据判别力（Major 3 review，V4.5；Major 7 修正，V4.6）】**：小语料下每轮 churn 很小、arena 可能根本不增长——无对照则"不实施 R4 也大概率收敛通过"，验收 12 无判别力。裁决：验收 12 加**对照组**——`GRAPHIFY_REBUILD_WORKER=0`（进程内管线）与默认（worker 模式）各跑 **20 轮异质变更集**，断言 worker 模式增量收敛 **且** in-process 对照组增量显著更大（判别力来自两臂差异，不来自绝对值）。**【Major 7 修正】**：V4.5 写的"同 mini 语料、同轮数"是**同质负载，无判别力**——判别机理是"同质 → 收敛、异质 → 不收敛"，而 mini 语料 + 同轮数恰是最同质的负载（增量 cache 下每轮只提 1 个文件、to_json 图大小恒定）→ 进程内臂也会收敛 →"两臂差异显著"恒不成立。修正：对照组与主臂都改用**逐轮异质变更集**（变更文件数 / 文件大小 / 增删混合在各轮间波动，模拟真实开发负载的分配模式交错）；判据改为**后 10 轮斜率**（前 10 轮为稳态建立期，不计斜率）+ **Private Bytes** 口径（working set 受系统修剪声学干扰，commit 口径才反映真实驻留）
- **逃生口（Major 3 review，V4.5 新增）**：R4/R5 补齐 US8"不改代码即可回滚"——新增 `GRAPHIFY_REBUILD_WORKER=0`（R4 禁用：flush 回退**进程内同步调用** `rebuild_worker.run_pipeline_payload(payload)`——worker 模块双宿主：CLI 入口（`-m`）+ 进程内函数调用，单份引擎代码、逃生口零额外维护，代价为 churn 落 serve=回滚到 R4 前行为）与 `GRAPHIFY_SESSION_EVICT=0`（R5 禁用：SessionEnd DELETE 端点存在但逐出降级为 no-op，注册表照记不逐出）。两者均 env 旋钮，默认开启 R4/R5，零代码回退。**【逃生口路径禁看门狗（Minor review，V4.6 按字面写就会错 #1）】**：`GRAPHIFY_REBUILD_WORKER=0` 路径下 watcher 是**同步调用**（无 spawn、无心跳文件、无 Popen 句柄）——**必须完全跳过 spawn 监控逻辑（心跳监听 / hang 判定 / `taskkill /T`）**，否则实现者保留看门狗框架时（popen 置 None 或误用 serve 自身 pid）会 `taskkill` **杀掉 serve 自己**。实施红线：watchdog 监控路径只在真 spawn 分支存在，逃生口分支是纯同步调用 + 异常直接冒泡到 flush 层既有恢复。**【逃生口整体停用看门狗（Minor B review，V4.6 补精确）】**：`GRAPHIFY_REBUILD_WORKER=0` 下要**整体停用看门狗**——不写心跳文件、不检 age、不 taskkill（进程内管线没有 worker pid，看门狗若照跑 `taskkill /T /PID <pid>` 杀的是 serve 自己——pid 落到 serve 或 None）。**【SessionEvict 逃生口注册表增长（Minor C review，V4.6 新增）】**：`GRAPHIFY_SESSION_EVICT=0` 下"注册表照记不逐出"若照记，长驻 serve 跨会话累积 session_id → roots **无界增长**。修正：**no-op 模式不注册**（`_session_register` 首行检查 env==0 直接返回，零增长零复杂度）；备选"注册即弃 / 条数上限 LRU"为逃生口临时状态引入架构复杂度，无必要

### R5：会话生命周期逐出（V4.3）

- **动机**：Claude Code 对话结束时释放对应资源。R4 后子进程短命（每批即退），常驻物 = per-project watcher（线程 + 注册表项）——"对话结束 → 杀对应子进程"的正确落点 = **会话结束 → 逐出该会话独占的项目 watcher**（复用 LRU on_evict 停机协议），SessionEnd 仅是把"容量压力逐出"多加"会话结束逐出"一个触发源
- **注册（Q4a 裁决）**：prompt_hook /query payload +1 字段 `session_id`（Claude Code hook stdin JSON 免费字段，此前未转发）；serve `_session_registry: session_id → set[resolved project_root]` + 反向索引。**注册非心跳**——hook 仅在结构性提问时联系 serve，稀疏不可靠，不承担活性判定
- **注销（Q4b 裁决）**：**显式通知**（SessionEnd hook → `DELETE /sessions/{id}`，接线形态见下条）而非心跳缺口启发式——UserPromptSubmit 稀疏（思考 30min 无 prompt 正常），缺口阈值在"误杀思考中会话"与"慢几小时"间两难，纯调参坑。**崩溃兜底**：SessionEnd 未发出（kill -9）→ 仍不做缺口启发式，骑 idle 自杀——3600s 无查询整体死亡，泄漏 watcher 最坏寿命 ≤1h **有界且零调参**
- **逐出（Q4c 裁决）**：DELETE → 注销 → 引用归零（无其他会话引用）→ `evict_graph(graph_path)`（复用 on_evict 停机协议：stop → final flush → join）+ **同步从 `_ctx_cache` 移除该项目的 entry（含图对象释放）**——否则图仍驻留缓存，US11 的"释放该会话全部内存"不成立；且下次查询若命中 stale 缓存（入口残留、backfill 未完成），会短暂服务旧图加并发重建。移除后下次查询走 `_select_graph` 全链路（惰性重挂 watcher + 门控判定 + load），干净重建。**【锁序实施红线（review 发现，serve.py:196 既有约束显式化）】**：缓存的同步移除（含 evict_graph 触发的 stop→final flush→join）必须在**缓存锁外**执行——与票 03 on_evict 同序（`_GraphContextCache.load` 在锁内收集 evicted 键、锁外回调）；锁内调 stop→join 会与 pipeline 完成回调抢锁死锁（serve_watcher 补丁完成回调与缓存加载同持 `_ctx_cache` 的锁）。实施时"移除 entry + evict_graph"组合为一个锁外动作。**在飞重建让它跑完**（落盘原子、产物对下个会话有益，杀掉纯浪费）；卡死由 R4 心跳杀，join 有界。**pinned 默认项目豁免**（生命周期属 serve 本体，归 idle 自杀）——serve 侧豁免判定：project_path resolve 后 == 默认项目 root（`serve_watcher.default_project_root`）。**【豁免落点（V4.10 定案）】：落在注册侧**——注册时即跳过、不入注册表，故逐出侧无从豁免（不注册者无需判逐出）；验收表 15"pinned 默认项目豁免（不逐出）"是注册侧判定的可观测结果，两者不冲突；实现不要另起逐出侧豁免分支
- **SessionEnd 接线最小形态（用户点名交付，比 prose 更精确的裁决载体）**：
  - `settings.json` 增一条 `SessionEnd` hook（与 UserPromptSubmit 同型 command；`timeout` 短设防卡对话退出——逐出失败静默，不重试）。**【接线实施红线（review 发现）】**：`~/.claude/settings.json` 的 `SessionEnd` **已有既有条目**（手动注册的 `sessionend-graphify-update.sh`，CLAUDE.md:44 明记三 hook 手动配置、sync.sh:119-124 显式维护"SessionEnd → sessionend-graphify-update.sh"存在性检查）——实施必须为 hooks **数组追加**而非整体覆盖，否则静默破坏既有 lifecycle（每会话结束的 graph.json 更新丢失）。检出既有 `SessionEnd` 块 → 在其中追加子项，或新建条目仅当键缺失
    ```json
    "SessionEnd": [
      { "hooks": [ { "type": "command", "command": "graphify prompt-hook --session-end", "timeout": 15 } ] }
    ]
    ```
  - prompt_hook `--session-end` 分支：读 stdin JSON 的 `session_id`（缺失静默退出）→ `DELETE http://127.0.0.1:{GRAPHIFY_MCP_PORT|8765}/sessions/{session_id}`（`--max-time 2` 类短超时）→ **一切失败静默**（server 已 idle 自杀则正好无需逐出；hook 永不因逐出卡对话退出）。**【超时口径关系（Minor F review，V4.6 新增）】**：settings.json `timeout: 15` 与 `--max-time 2` **不冲突**——15 是 hook **总时长上限保护**（Claude Code 对超时 hook 的杀灭阈值），2 是 hook **主动放弃等待**（server 侧请求继续跑完、hook 立即返回不卡对话）；2 < 15 保证 hook 从不被 Claude Code 强杀
  - serve 新端点 `DELETE /sessions/{id}`：注销 → 逐出引用归零项目；返回 `{status, evicted[]}`。**【端点吞吐红线（Minor review，V4.6 按字面写就会错 #3）】**：端点必须用 **sync `def`**（FastAPI/Starlette 自动放线程池执行）或显式 `asyncio.to_thread` 委托——逐出动作含 watcher 的 stop → final flush → join（秒级阻塞），若写 `async def` 并在请求协程内直接执行阻塞逐出，会**阻塞整个事件循环**，所有 `/query` 全部挂住（serve 是单进程单 loop）；sync def 语义 = 逐出耗时只占线程池线程，事件循环并发不受影响。测试用真实 server 打 `DELETE` 时顺带断言逐出期间 `/query` 仍即时响应。**【G 强化（Minor G review，V4.6）】**：R4 下逐出不再只是进程内 final flush——在飞批次的 worker 是**子进程，逐出需 spawn worker 完成批次并等待（可达心跳阈值 60s+）**，阻塞窗口比 R4 前更大 → sync `def`（FastAPI 线程池）是**硬性要求**；且逐出全程复用 `_ctx_cache` **锁外序**（R5 逐出锁序红线：stop → final flush → join 与缓存移除都不持缓存锁执行，防与 pipeline 完成回调抢锁死锁）；回归断言同步加长超时用例（逐出含 worker 等待 ≥60s 时 /query 仍即时响应）
- **盲区（Q4d 裁决）**：MCP-only 会话（直连 MCP 工具、从不触发 prompt_hook）不在注册表，其项目可被别的会话 SessionEnd"误逐出"——后果 = 下次调用惰性重挂 + 门控跳过（≤2s 扫描），**优雅降级零正确性问题**。**【后果补全（Minor 4 review，V4.5）】**：除重挂代价外，**被误逐出的项目 watcher 停止期间（直到下次查询惰性重挂回），该会话对项目文件的编辑不再自动重建进图**——功能层面的可见差异（实时性暂缺），非正确性问题（重挂后门控判定陈旧即补齐）；本仓场景下 MCP-only 会话使用 serve 的机会本就少、且误逐出需"另一个会话恰在同项目上 SessionEnd"，实际命中面极窄。spec 注记接受
- **免依赖**：R5 与 R4 并行（"在飞 worker 跑完"在 R4 前指进程内管线 final flush，语义一致）

### 实施顺序

```
R-E ‖ R3（并行，互不依赖）→ R1（收尾）
R4 ‖ R5（并行，互不依赖；V4.3 增补）
```

## Testing Decisions

- **最高接缝**：验收 1 直接驱动 `_GraphContextCache.load()` 两次（构造 key 变化）测 tracemalloc 峰值，无需 server 进程插桩
- **prompt_hook 失败分支测试（用户发现 3，新增）**：mock `_query_via_http` 抛连接异常 → 断言 ensure-server 被非阻塞调用**恰好一次** + 该条返回本地回退结果 + 下条恢复 HTTP 的状态语义——prompt_hook.py 现零测试保护，本票起建立回归网
- **测试适配清单（预置，防误诊）**：票 04 新鲜夹具的"挂载即补齐"测试改 `GRAPHIFY_BACKFILL=always` 或注入真实陈旧；`test_lazy_mount_backfills_stale_graph` 与 self-heal E2E **零改动保留**（有真实陈旧，门控正确性活证）
- **R4/R5 测试接缝（V4.3）**：R4 用 `sys.executable -m graphify.rebuild_worker` 直驱（伪造 stdin payload 单行 JSON）测管线等价（含删除/seed/纯补齐 force 语义），fake `taskkill` 插桩测心跳杀树；**收敛性（验收 12）用逐轮异质变更集构造驱动**（mini 项目十文件级基础语料、单轮 <5s，每轮变更文件数/文件大小/增删混合波动，模拟真实开发负载的分配模式交错；worker 模式与 `GRAPHIFY_REBUILD_WORKER=0` 进程内对照**两臂同跑 20 轮**，判据 = **后 10 轮 Private Bytes 斜率**——前 10 轮稳态建立期不计；单轮 <5s 保证 2×20 轮判定在测试时间预算内完成）——全量重建级（97-119s/轮）不可直接跑 20 轮，其验证由单轮 worker 退出后 parent RSS 回落断言替代；R5 用测试 server 直驱 `_session_register`/`_session_end` + 真实 WatcherRegistry 测逐出（含 **_ctx_cache entry 同步移除断言**与**锁外移除序断言**），settings.json 接线用配置断言 + fake urllib 测 `--session-end` 分支失败静默
- **验收表**：

| # | 项 | 标准 |
|---|---|---|
| 1 | R-E 峰值 | load() 两次驱动，重载 tracemalloc 峰值砍 ≥ 一个图（~50MB） |
| 2 | R-E 逐出红线 | 重载刷新不触发 on_evict（watcher 仍 alive） |
| 3 | R-E 并发 | 重载窗口内并发 /query 阻塞 ≤2-4s 后正常返回，无 404/500 或空结果（见 R-E"阻塞窗口"条款） |
| 4 | R-E 失败 | corrupt graph：当次报错（G 停留 None）；cache pop 无 None-hit；下查自愈 |
| 5 | R3 idle | `--idle-timeout 2` 静默 3s → 退出 + stop_all + final flush 落盘 |
| 6 | R3 自愈 | server 死后 prompt 实测：首条本地回退 + ensure-server 恰一次；次条恢复 HTTP |
| 7 | R1 门控 | 五路：新鲜跳过 / 陈旧执行 / touch 误报执行 / 删除触发（count）/ 超时回退 |
| 8 | R1 适配 | 适配清单落地；stale 测试零改动通过 |
| 9 | 整体内存 | 单项目 ≤250MB；多项目 5 轮换 ≤400MB 且新鲜重挂零重建（**口径注记 V4.5：与验收 12 正交**——9 为绝对峰值下界（US1，含一次重载循环），12 为连续重建增量收敛判据，各自独立标定不互斥） |
| 10 | 回归 | test_serve / test_serve_http / test_watcher_registry / test_mount_backfill_lock / test_serve_watcher / test_rebuild_entry 全绿；check-custom 以 `--skip-global` 运行 exit 0 全过（**V4.3 修订：`--skip-global` 已实施（285a52a），非"顺带未实施"**——PATH/版本类环境项降级 warning 不计数，仓库存在性硬检查不受影响） |
| 11 | 隔天 | 新会话起始 RSS 与前日无关 |
| 12 | R4 收敛性 | 主臂与对照臂**同用逐轮异质变更集**（mini 项目语料 + 每轮变更文件数/大小/增删混合波动，单轮 <5s；前 10 轮稳态建立期不计斜率）连续 ≥20 轮后，断言 worker 模式**后 10 轮 Private Bytes 斜率趋平**（无线性爬升，平台判据替代单点绝对值）**且与 `GRAPHIFY_REBUILD_WORKER=0` 进程内对照臂差异显著**（判别力来自两臂差异，不来自绝对值——同质负载两臂恒收敛无判别力，Major 7）；worker 退出后 parent RSS 回落（churn 随进程消亡，单轮实测替代全量 20 轮——全量 97-119s/轮不可直接跑 20 轮） |
| 13 | R4 心跳 | 卡死 worker 被 `taskkill /T` 杀树 → 批次按失败退避 restore（不丢事件）；正常 worker 不误杀、心跳文件退出时清理；**心跳基线按 spawn 时刻重置（上批强杀残留心跳不误杀新 worker，Minor 1）** |
| 14 | R4 锁失 | **两探落点（V4.7）**——闸前 `probe_lock` 一次（锁忙不占全局闸，与 Major 1 同构）+ 持闸后 spawn 前再探一次，锁忙即退避**不 spawn**（Major 2）；**判活接管（V4.6/V4.8）**——`_acquire_lock` 接管条件升级 `age > _LOCK_STALE_S or not _pid_alive(pid)`（四态判定表见 R4 锁让渡条），taskkill 遗迹锁经判活即回收（Major 4）；`probe_lock` 只报告不删锁（V4.9 契约）；exit 3 引用 `EXIT_LOCK` 常量（Minor 3）；收敛后重试指纹命中跳过（不重复 build） |
| 15 | R5 会话 | /query 注册 → SessionEnd DELETE → 引用归零项目 watcher 逐出（stop + final flush 落盘）**且缓存 entry 同步移除**；在飞重建跑完；pinned 默认项目豁免（不逐出） |
| 16 | R5 接线 | settings.json SessionEnd hook 存在且调 DELETE（timeout 短设）；prompt_hook `--session-end` 分支失败静默（server 已死时无异常） |

## Out of Scope

- 字段分层（-36%/图）：有工具消费，逐工具核对回归成本 > 收益
- CSR 紧凑结构：动全部查询读点；4.4× 是 Python 结构性成本
- data 字典削峰：R-E 后剩余峰值结构性；idle 归零兜底
- watcher flush 降频：触发面语义正确，不为内存扭曲行为
- LRU cap 收紧：`GRAPHIFY_MAX_CONTEXTS` env 为运行时旋钮兜底（V4 评审移出——idle+evict 后单窗口驻留需 8 项目才触发，真实使用 ≤4）
- 进程内管线的碎片级优化（arena/calloc 调优/MALLOC_ARENA_MAX 等）：R4 出进程化后无意义，churn 随 worker 消亡

## Further Notes

- **工具化（V4.3 修订：随票顺带已完成，非待办）**：check-custom.sh 增加 `--skip-global` 开关，把 PATH/版本类环境项降级为 warning 不计数，恢复 exit 0 的自动化守护语义——**已实施于 285a52a（Task 2 末尾）**；现状 check-custom 以 `--skip-global` 运行 exit 0，仓库内存在性硬检查不受影响
- **部署**：editable 安装实证生效——手动 kill 重启一次，后续 sessionstart/ensure-server 自愈拉起；Get-Process 探针观察 RSS
- **回滚**：`GRAPHIFY_IDLE_TIMEOUT=0`（关 idle）+ `GRAPHIFY_BACKFILL=always`（恢复无条件补齐）零代码回退；R-E 纯内存优化无行为变化，git revert；**R4 逃生口 `GRAPHIFY_REBUILD_WORKER=0` / R5 逃生口 `GRAPHIFY_SESSION_EVICT=0`（V4.5，环境级回退）**；高驻留场景兜底旋钮 `GRAPHIFY_MAX_CONTEXTS`
- **根因记录**：水位累积（主因）/ 真实工作集 ~180MB（合理成本）两层定稿
- **P2 注记（V4.9，记一笔不实施）**：长耗 worker 与年龄接管——接管条件 `age > _LOCK_STALE_S or not _pid_alive(pid)` 的**前半段**会在 **worker 正常但耗时 > `_LOCK_STALE_S`=600s** 时被误接管。属**既有语义非回归**（R4 前 hook 面同样存在；当前全量重建 72-120s 远低于 600s 不触发），R4 后 worker 成主重建路径、暴露面变大。可选优化：**心跳 fresh 时年龄接管让位**（心跳文件更新即视为 live，年龄分支让位给判活判定）；当前不做
