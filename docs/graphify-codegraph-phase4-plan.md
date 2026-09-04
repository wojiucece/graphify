# graphify × codegraph Phase 4 深化方案（融合 jCodeMunch 吸收项）

> ## ⚠️ 已被 native-indexing 取代（2026-09-04 收尾）
>
> 本方案（v1.15，codegraph-merge 深化轨道：serve 15 工具 / ranked / 金标 /
> dispatch / session_snapshot / structure_queries / git_symbols）**已由
> `docs/graphify-native-indexing-spec.md`（原生索引能力，codegraph 运行时退役）取代**。
> 其可吸收机制（点查契约 / 融合排序 / 诚实性信封 / 金标闸门等）在原生链路上以
> `.fts-index.db` 缓存 + graph.json 事实层重建；codegraph 依赖整体退役。本文档保留为
> 历史决策记录，不再作为实施依据；决策链见 `docs/wayfinder/MAP.md` 与
> `docs/adr/0001-retire-codegraph-runtime.md`。

**版本**：v1.11（2026-09-01 实现清晰度质询 14 问裁决落档——地基级修正 4：①`_meta` 形态改全工具统一尾部行（v1.7 的 JSON 附加键主形态对全文本工具面无落点，实测 10 工具全返回纯文本）；②codegraph DB/切片文件/状态文件路径从 active graph 的 project_path 推导（多项目热切换联动总条款，此前所有直查条款默认单项目）；③C 轨道交付形态定 MCP 工具（消除 CLI/MCP 双写矛盾）；④A4 重构为 rebuild 后实时总结注入（删 diff 摘要与查询历史，内容收敛为 god nodes + 社区标题二元组，正确性由构造保证）。实现协议 10：点查 verdict 语义、B1 缺省预算 2000、fan-out 无所属类降级、`_redact` 覆盖 querylog、金标集构成、dispatch_candidate 双判据并集（confidence<0.9）、合并边最保守取值、状态文件单写者、enum+isError 参数校验、效率基准三条协议）。v1.12（2026-09-01 增补：B4 cache GC 独立小节——产物重叠核查结论（疑似重复项全部有正当消费者，无可合并项）+ manifest 锚定 mark-and-sweep GC 七要点；R19 登记）。v1.13（2026-09-01 A4 机制事实修正——①“hook 同步等待 rebuild”是错误描述（Phase 3 实况是 nohup detach，PreCompact timeout 实配 30s，同步必超时强杀），改零等待：快照读当前磁盘 graph.json 实时计算；②PreCompact 无注入通道（官方文档：stdout 只进 transcript、stderr 只给用户），v1.11 的 systemMessage/additionalContext 二选一前提错误——注入走 SessionStart + matcher:compact（官方确定通道），快照文件从 fallback 升格为主机制；③B1 金标集拆批：10 条矩阵最小集先行 + 10 条 querylog 真实查询补齐）。v1.14（2026-09-02 实现落档——SDD 37 commits 全分支终审（With fixes → fix wave → re-review 全绿）合并进 v8-custom `b3ea9ed`。三个交叉偏差注册：①§5-C3 graph_diff 回退从"图内对比"改为**诚实空标记**（无法锚定 git 基线时 basis="graph_diff" + 空结果 + serve 判 absent——run_analysis.diff 需前置图快照，首次/孤儿无从对比；naive 文件集对比 fork 实测假阳性 6 个）；②`_digraph_view` 生产 str 路径修复（I4：active_graph_path 恒 str，Task 9 仅 Path 测试的集成盲区，Task 12 闭包实测撞出——入口 Path() 归一化）；③C2 `patterns` 参数不暴露给 MCP（inputSchema 空 properties，description 诚实声明非 Python 约定不支持）。另登记：R19 B4 已实现且 live 重算锚定（file_hash 复用 cache.py 公开函数）；`_digraph_view` 去留裁决待合并后做（`_load_graph` 强制 directed:True 返回 DiGraph，docstring 原"无向化丢方向"叙述为错误前提）。v1.15（2026-09-02 follow-up 收尾落档——①A4 部署实测：真 /compact 闭环验证通过（PreCompact 落盘 graphify-out/.session-snapshot.json + SessionStart matcher:compact 注入逐字一致，hook 热生效无需重启）；②Q14 C2 20 符号人工抽样通过：判定函数 16,717 条全量交叉验证 mismatch=0 + `_edge_dispatch_info` 聚合链路 20 样本全 OK（覆盖 resolvedBy 命中集/None/confidence<0.9/多行并组 min-any 全分支）；③金标 querylog 升格裁决=暂缓：真实 fork querylog 语料仅 4 条可用且主题单调（hook 面），plan"declared 语料足够才升格"前置未满足——已开 `GRAPHIFY_QUERY_LOG_ENABLE=1` 积累（日志按 corpus 过滤 fork 查询），待攒够 10 条再全量升格；④`_digraph_view` 去留裁决=保留+修 docstring：确认 serve `_GraphContextCache`（G+communities，B1/query/get_node/depth=1）与 ranked `_GRAPH_CACHE`（lazy digraph，B3/C）双链路架构，`_digraph_view` 是 serve→ranked 桥非冗余——docstring 错误叙述修正（磁盘 directed:false 为逻辑标志，`_load_graph` 强制 directed:True 仅 renderers 弧序 #2309，方向与 get_digraph 等价）；⑤cli.py:1311 querylog `_redact` 对称（终审 T3）：query 命令 log_query 落盘 result 过 serve._redact，端到端验证 log response==_redact(_result) 精确相等。）。v1.10（2026-09-01 精简：删分析叙事与已具备前置，保留方案细节；完整修订史见 git log）。v1.9：B3 三件套——分发线索第三例直查（非 adapter 透传）、fan-out 查询期重展开（calls×extends×contains）、blast-radius（get_neighbors 扩展）。v1.8：B2 两步经济模型（get_node 扩展 include_source 四档，缺省 signature）。v1.7：grilling 19 问裁决落档。v1.6：基线 `5f600dd` 重审。
**日期**：2026-08-30
**基线**：

- 合并方案报告 v4.2（`docs/graphify-codegraph-merge-plan.md` §7 Phase 4，原文仅两条：跨模态层、双向写回否决）
- 执行计划 v3（`docs/superpowers/plans/2026-08-28-codegraph-graphify-merge.md`，Phase 0–3 共 16 Task）
- jCodeMunch 吸收分析（[jgravelle/jcodemunch-mcp](https://github.com/jgravelle/jcodemunch-mcp) @ main，2026-08-30 核读 README / CAPABILITIES / UNDER_THE_HOOD / AGENT_HOOKS 四份文档）

**定位**：Phase 0–3 完成后的可选深化轨道。本方案将原 Phase 4 愿景与 jCodeMunch 可吸收机制融合为五个轨道，全部在现有分层融合架构内自建——**抄机制，不抄代码，零依赖引入**。不改变"codegraph 符号事实层 + graphify 分析理解层 + 只读适配器"的总体架构，不触碰 Phase 0–3 已定决策（id 直传、watch 拓扑、种子迁移、双向写回否决）。

---

## 1. 吸收决策框架

### 1.1 原则

1. **机制优先于代码**：吸收的是设计模式（verdict 契约、通道融合、计量纪律），不是实现。
2. **零运行时依赖**：不引入 jCodeMunch 包，所有能力在现有 Python/TS 栈内自建。
3. **落点分层**：MCP 层（serve.py）承载检索诚实性与安全；分析层承载结构查询；fork 自定义面（scripts/）承载 git 轴等新能力轴，graphify 包尽量零触碰（沿用"自定义面与上游零交叠"惯例）。
4. **与 jCodeMunch 的关系是互补不是竞争**：它无分析层（社区发现、知识缺口、语义种子），我们无检索诚实性信封与通道融合——吸收项恰好填我们缺的半边。

### 1.2 吸收 / 不吸收清单

| jCodeMunch 机制 | 判定 | 落点 | 轨道 |
|---|---|---|---|
| verdict 契约（ok/low_confidence/absent/degraded）+ per-symbol freshness + 校准 confidence | ✓ 吸收 | serve.py 响应信封 + rebuild_entry 状态文件 | A1 |
| 响应自动脱敏（云厂商/JWT/GitHub/私钥 + AI provider 与 agent 密钥扩展） | ✓ 吸收 | serve.py 序列化出口 | A2 |
| WAL 单事务快照读取（"one immutable snapshot"语义） | ✓ 吸收（若 Phase 3 执行时未顺手采纳） | adapter/rebuild_entry | A3 |
| PreCompact 会话快照注入 | ✓ 吸收 | precompact hook + 新快照脚本 | A4 |
| ranked_context 多通道融合（BM25×中心性×精确名 pinning×token 预算） | ✓ 吸收 | 新 MCP 工具，FTS5（codegraph）× god_nodes（graphify） | B1 |
| 两步经济模型（search_symbols 元数据 → get_symbol_source 精确切片，不吐整文件） | ✓ 吸收（融合形态：不新增工具） | get_node 扩展 include_source 参数 | B2 |
| 调用边解析置信度（resolvedBy/confidence metadata）与动态分发 fan-out | ✓ 吸收（直查 + 查询期重展开，非 adapter 透传） | 边级直查 + calls×extends×contains 图遍历 | B3 |
| blast-radius 影响面分析 | ✓ 吸收（参数扩展形态：不新增工具） | get_neighbors 扩展 direction/depth | B3 |
| find_dead_code / get_untested_symbols | ✓ 吸收 | 分析层纯图算法 | C1/C2 |
| get_changed_symbols（git diff→符号） | ✓ 吸收 | scripts/ 新 git 轴 | C3 |
| get_hotspots（复杂度×churn） | ✓ 吸收（复杂度用代理值） | 依赖 C3 | C4 |
| measured/declared 出处纪律 + 计量只向下误差 + schema token 预算 | ✓ 吸收 | 跨轨道工程纪律 | §8 |
| SCIP 编译器证据导入 | ◐ 押后 | 适配器旁挂 DB，成本高 | E |
| MUNCH 二进制线格式 | ✗ 否决 | 破坏 MCP 客户端互操作 | §11 |
| 90+ 工具面 | ✗ 否决 | 它自己都需要 tool tiers 自救；以 schema 预算纪律代替 | §8 |
| 遥测计数器 / 商业授权体系 | ✗ 无关 | — | §11 |
| 双向写回 codegraph | ✗ 否决（沿用 v4.2 原决策） | 侵入 schema 与迁移体系，收益不明确 | §11 |

---

## 2. 轨道总览

| 轨道 | 工期 | 难度 | 主要技术不确定性（v1.5 验证后残留） | 核心收益 | 收益 |
|---|---|---|---|---|---|
| Track A 诚实性与安全信封 | 2–3 天 | ★★☆☆☆ | 仅剩 A4 注入 schema 二选一（systemMessage vs hookSpecificOutput.additionalContext，本机实测可直接观察效果）；A1 出口落点 / A2 正则 / A3 快照机制均已实测 | 全响应可判信；重建期不误导 agent；密钥零外泄 | ★★★★☆ |
| Track B ranked_context 融合检索 + B2 符号切片（get_node 扩展）+ B3 分发线索/blast-radius（get_neighbors 扩展） | 4–6.5 天 | ★★★★☆ | FTS5 表结构 / bm25 语法 / 列填充率 / id 折叠（实测 0 碰撞）均已消除；**残留 = 融合排序质量**（20 查询金标集，实现时验证）与 query_shape 分析器设计 | 合并栈旗舰工具：两侧引擎首次在同一查询内融合 | ★★★★★ |
| Track C 分析层结构查询 | 4–6 天 | ★★★☆☆ | C1 入口策略有效性（原型实测 86.4% 误报 → 修正已设计 + >50% 闸门）；C3/C4 的 git 轴无前置依赖、机制已补基线锚定；git 轴自定义面 = 未来事件，只能门控 | 原生工具答不了的结构问题 + "这次图里变了什么" | ★★★★☆ |
| Track D 跨模态双层图 | 1–2 周起 | ★★★★★ | LLM 成本与输出确定性（原文评级保留，不可提前验证） | 代码↔docs↔图像双层图、跨模态惊喜连接 | ★★★☆☆ |
| Track E SCIP 证据导入 | 不排期 | ★★★★★ | CI 产索引 + 双证据合并 | TS 项目编译器级引用证据 | ★★★☆☆（押后） |

**推荐节奏**：A（2–3 天，独立可先行）→ B（3–5 天，复用 A 的信封设计；B2 切片扩展薄层另计 0.5–1 天，可拆独立先做——零新索引零新管线）→ C 按需 → D 远期可选。**C 轨道内部依赖边（v1.7 补，Q14 裁决）**：C1 的入口修正（import 推导等）是 C2 的前置——边语义不完整是两者共享根因，C2 不得先于 C1 修正落地；C3 无此依赖可独立。A 与 C 无耦合可并行；B 依赖 A1 的 `_meta` 信封字段先定形。**C 轨道内部排序（v1.5 按实测数据重估）：C3 > C2 > C1**——C3 收益最明确且无入口歧义；C1 因原型实测的库型入口问题收益条件化（须过 >50% 闸门才成立，入口探测占其工期一半），若工期紧可降级为"孤儿符号提示"。**沿用 TDD 模式**：每任务先写测试（失败）→ 实现 → 通过 → 提交。

---

## 3. Track A — 检索诚实性与安全信封

### 动机

痛点：rebuild 期间（分钟级窗口）serve 返回半旧图，agent 无法区分"图中没有"与"图正在重建"。方案：响应挂 verdict（`absent` 携扫描计数、与 `degraded` 严格区分）+ freshness + confidence 三组机器可读信号。原料齐备：serve 惰性 mtime 热重载 + rebuild_entry 状态。

### A1 verdict + freshness 响应信封

**Files**：
- Modify：`scripts/rebuild_entry.py`（写状态文件）
- Modify：`graphify/serve.py`（响应统一过信封）
- Create：`tests/test_response_envelope.py`

**设计要点**：
- rebuild_entry 在 `rebuild()` 入口写 `graphify-out/.rebuild-state.json`（`{"phase": "rebuilding", "started": ts, "schema": 1, "db_fingerprint": ...}`），`finally` 中更新为 `{"phase": "complete", "last_duration": ...}`（与锁清理同一 finally 块）。
- **写权单一化**：状态文件仅允许持有 mkdir 锁的进程写（写前三查锁 owner pid 即自身）；serve 侧只读不写（时效逃生只影响内存判定）；锁与状态文件同一 finally 块释放/更新，天然原子。
- **路径联动总条款（多项目）**：codegraph DB 路径、切片文件基准、状态文件路径一律从**当前 active graph 的 project_path 推导**——`_select_graph` 切换时同步重绑 DB 路径与状态文件路径，serve 启动时的缺省 graph 同理取其 project 根；B2/B3 的直查与切片条款均引用本条。
- **崩溃时效逃生（v1.7 补，Q3 裁决）**：kill -9 / 断电不执行 finally，状态文件会永久滞留 `rebuilding` → 之后所有查询永久 degraded。serve 侧读取时加时效判定——`started` 距今超过 `max(2 × last_duration, 30min)`（`last_duration` 由 rebuild 成功路径写入同一文件）→ 视作 `stale_index` 而非 `rebuilding`，日志留痕。自愈上限 30 分钟，不依赖人工干预。
- **schema 版本化（v1.7 补，Q19 裁决）**：状态文件首字段 `schema: 1`，演进纪律“只增不改”——新增字段必须自带缺省语义，旧读者忽略未知字段（JSON 天然支持）；字段表以 A1 为 owner、C3（git_head）为 contributor，两轨道引用同一份定义。
- **isError 边界**：`absent`/`degraded`/`low_confidence` 是诚实回答不是错误——走正常响应携带 `_meta`，不得标 isError；真错误走 isError（upstream 兼容层已有该路径）且不附 `_meta.verdict`。
- serve.py 检索型响应统一附加 `_meta`（**检索型工具清单在 A1 Task 细化时逐一枚举**，判据 = 响应内容反映图数据现状；新增工具不进清单的实现拒绝合并）：
- **verdict 三分规则（扫描型工具）**：`scanned_nodes > 0` 且无命中 → `absent`；`scanned_nodes = 0` 且非 rebuilding → `absent` + `_meta.empty_graph = true`（合法空图：全新项目/纯文档项目）；rebuilding 窗口 → `degraded`（优先级最高）。
- **点查工具语义**（get_node：节点在图 → `ok`；不在 → `absent`（`scanned_nodes: 1`）。遍历型（get_neighbors/B3 扩展）：起点存在 → `ok`，遍历中遇低置信边时**结果级**标 `low_confidence`（逐边信息放 `dispatch_candidate` 字段，不逐边改 verdict）；起点不存在 → `absent`。
- **_meta 注入形态 = 全工具统一尾部行**（v1.11 定夺；实测 10 工具全返回纯文本，JSON 附加键主形态无落点）：所有文本响应末尾追加一行空行 + `_meta: {"verdict": ..., "freshness": ...}`（单行 JSON 紧贴文末）；B1/C 等新 JSON 工具同样在序列化后追加同款行——**全工具面单一格式，不做 JSON 内嵌键**。解析约定：agent 按行首 `_meta:` 前缀识别。既有正文逐字节不变（回归锁定“正文不变”）。
- `verdict ∈ {ok, low_confidence, absent, degraded}`、`freshness ∈ {fresh, stale_index, rebuilding}`。推导：状态文件 phase + graph.json mtime vs `.codegraph/codegraph.db-wal` mtime（主 DB mtime 仅 checkpoint 时更新，不可直接用；`-wal` 不存在时回退主 DB，取较新者）。
- `absent` 必须携带扫描计数（本次实际考察的 nodes/edges 数）——"没有"是有证据的主张，不是耸肩。
- `rebuilding` 窗口内一切检索结果标 `degraded`，agent 据此决定等待而非下"不存在"结论。
- confidence 初期一律标 `declared`（工程先验），不冒充 `measured`（纪律见 §8）。

**验收**：
- [ ] 全部检索型工具响应携带 `_meta.verdict` 与 `_meta.freshness`（schema 断言测试）。
- [ ] 手动放置 rebuilding 状态文件 → 查询返回 `degraded`（单元测试直调 serve 层函数）。
- [ ] 空结果返回 `absent` 且（`scanned_nodes > 0` 或 `empty_graph = true`）——合法空图（全新项目/纯文档项目）走 empty_graph 分支（Q9）。
- [ ] 状态文件在异常路径下不留 `rebuilding` 残留（finally 覆盖测试）。

### A2 响应自动脱敏

**Files**：
- Modify：`graphify/serve.py`（序列化出口统一过 `_redact()`）
- Create：`tests/test_redaction.py`

**设计要点**：只在响应出口层做，不改磁盘产物。**`_redact` 出口统一**：serve 返回与 querylog 落盘（querylog 记录 result 全文，不脱敏则密钥进磁盘日志——侧门挪位）共用同一 `_redact()` 函数。**性能门**：30 条编译正则逐条扫 ~50KB 文本为毫秒级，验收含 10KB 样例 < 10ms 门。**范围边界（Q5）**：仅 MCP 通道 + querylog——磁盘产物（graph.json/GRAPH_REPORT.md）不脱敏（agent 直读文件是敞开侧门；接受：改磁盘违反只读章程、破坏 graph_diff 一致性）；Track D 引入 LLM 遍历再评估。语义层采集物（AGENTS.md/`.env.example`/config 注释）恰是 AI 密钥最高频泄露类别，模式集以 AI 密钥为一等公民。**两级模式集**：

- **L1 厂商前缀类（默认启用）**——前缀 + 长度 + 字符集三重校验，误报率近零：

| 提供商/来源 | 模式要点 |
|---|---|
| OpenAI / DeepSeek | classic `sk-(?!ant-|proj-)[A-Za-z0-9]{32,}`（负向先行排除 ant/proj；DeepSeek 32 位 hex 天然被此规则覆盖，kind 标注兼容）；project 型 `sk-proj-[A-Za-z0-9_-]{40,}`；`sk-svcacct-`/`sk-None-` 前缀 |
| Anthropic | `sk-ant-(api\|admin)\d+-[A-Za-z0-9_-]{80,}`（实测格式带版本号，如 `sk-ant-api03-`） |
| Google Gemini | `AIza[0-9A-Za-z_-]{35}` |
| HuggingFace | `hf_[A-Za-z0-9]{34,40}`（官方文档 34 位；扫描器实践放宽至 40） |
| Groq | `gsk_[A-Za-z0-9]{48,}`（gitleaks 规则 48 位；控制台实际生成 52，取下限开放） |
| OpenRouter | `sk-or-(v1-)?` 前缀 |
| xAI | `xai-[A-Za-z0-9]{80}`（gitleaks 规则） |
| Perplexity | `pplx-` 前缀 |
| Tavily | `tvly-` 前缀 |
| Together | `tgp_v1_` 前缀 |
| Replicate | `r8_` 前缀 |
| LangSmith | `lsv2_(pt|sk)_` 前缀 |
| Slack（MCP 集成常见） | `xox[bpars]-` 前缀 |
| GitHub Copilot | `ghu_` / `ghs_`（并入 GitHub 系 `ghp_/gho_/github_pat_`） |
| 云厂商/通用 | AWS `AKIA[0-9A-Z]{16}`、GCP/Azure 服务账号、PEM 私钥块、JWT（`eyJ` 三段式） |

- **L2 泛型启发式（默认关，逐条校准后启用）**——覆盖无固定前缀的密钥（Mistral、Cohere、Azure OpenAI 32 位裸串、Cursor 等）：`Bearer\s+[A-Za-z0-9._-]{20,}`；键名高熵值组合 `(?i)(api_?key|token|secret|password|credential)\s*[:=]\s*['"]?[A-Za-z0-9_\-]{20,}`。普通 base64 / URL / 代码 token 误伤风险高，须先过误伤样例集。

每条模式携带 `kind`，替换输出 `[REDACTED:kind]`；表驱动常量 `_REDACT_PATTERNS`，新增 provider 只加一行。降级原则：漏报可接受，误报不可接受（误替换破坏 agent 理解）。六条主格式已对照 gitleaks/secretlint 等规则库核实，其余为前缀唯一性强的常识格式——正则待 fixture 校准（国内 provider 用 `sk-` 前缀，classic OpenAI 规则天然覆盖）。

**验收**：
- [ ] L1 全模式 fixture 命中测试（每条至少一正例一边界负例，如 `sk-` 短串）。
- [ ] 误伤样例集：普通 base64、URL、长代码 token 不误报（固定样例回归）。
- [ ] 节点/边属性含假密钥的 fixture，经 serve 序列化后全部被替换为 `[REDACTED:kind]`。
- [ ] 新增 provider 只改一行常量的扩展性测试。

### A3 WAL 单事务快照读取

**Files**：
- Modify：`scripts/adapter.py`（若 Phase 3 执行会话已顺手采纳则本任务勾销）

**设计要点**：`load_codegraph` 的读取包进单个 `BEGIN ... COMMIT` 读事务——WAL 下读事务天然快照隔离（实测：并发写 + checkpoint 期间事务内计数保持快照值）；Python sqlite3 默认 autocommit 下 SELECT 各自独立，须显式 `BEGIN`。rebuild_entry 两轮指纹循环降级为兜底信号（保留，不再承担一致性主责）。

**降级链（Q11）**：`mode=ro` 打开 WAL 库需 -shm，只读介质上 OperationalError——降级 `immutable=1`（只读介质无并发写，陈旧快照顾虑不成立）→ 仍失败才报错；降级写日志。不触碰 v4.2 读写场景裁决。

**验收**：
- [ ] 并发测试：读取期间另一进程写入 DB，读取计数与事务前后快照一致。
- [ ] 指纹收敛循环保留但两轮语义改为"检测到变化记录日志"而非重跑。

### A4 PreCompact 会话快照注入

**Files**：
- Modify：`scripts/precompact-graphify-update.sh`
- Create：`scripts/session_snapshot.py`
- Create：`scripts/sessionstart-graphify-compact-inject.sh` + 注册进用户 settings.json（SessionStart, matcher: compact）

**内容 = 纯图事实二元组**（v1.11 重构）：god nodes top-N + 社区标题 top-N，压至 ~500 tok。**显式排除**：① diff 摘要（外部变更感知是 C3 职责——rebuild 附加报告进当时活跃会话；PreCompact 会话需要“图现在是什么”而非“图变过什么”，god nodes/社区标题本身即最新状态的函数，天然覆盖外部变更）；② 会话内查询历史（Compact 自身主业，做即重复且更差——没有对话全文）。**机制 = rebuild 后实时计算**（替代预产快照，正确性由构造保证——读的就是刚重建的图，无快照过时问题）：hook **零等待**（Phase 3 实况：PreCompact 以 nohup detach 触发 rebuild 后立即返回，timeout 实配 30s——同步等待分钟级 rebuild 必超时强杀致锁残留，见 precompact 脚本 E 裁决注释）：`session_snapshot.py` 直接读**当前磁盘 graph.json** 实时算 top-N 度数 + 社区标题（networkx 加载排序，秒级；watch/SessionEnd 高频触发保证图通常接近新鲜，恰处 rebuilding 窗口则注入内容标 degraded）。rebuild 照常 detach 触发，为下一次会话服务。**注入通道（v1.13 修正，官方文档核查）**：PreCompact 无注入能力（stdout 只进 transcript、stderr 只给用户）——两段式：①PreCompact hook 把实时算出的快照写 `graphify-out/.session-snapshot.json`（点前缀非 .md，不进提取管线，**主机制**非 fallback）；②新增注册 SessionStart hook（matcher: compact）读快照 → stdout 注入（官方确定支持通道）。本机实测内容从“schema 二选一”降级为“验证注入效果”。**降级**：graph.json 缺失/损坏 → 写空快照 + 日志，压缩流程不受影响。

**验收**：
- [ ] compact 后新会话凭快照可回答"当前项目核心模块是什么"类问题。
- [ ] hook 失败（如 graph.json 缺失）时压缩流程不受影响。
- [ ] 快照计算时限实测 < 10s（PreCompact timeout 30s 内安全余量）。
- [ ] SessionStart(matcher=compact) 注入后压缩首问可引用快照内容（本机实测注入效果）。
- [ ] PreCompact hook 端到端耗时不随 rebuild 时长变化（零等待：detach 后 hook 即返回）。

---

## 4. Track B — ranked_context 融合检索（旗舰）

### 动机

FTS5 在 codegraph、中心性在 graphify——两侧引擎现成，缺的只是融合工具：BM25 词法 × 图中心性 × 精确名 pinning × token 预算。

### B1 get_ranked_context MCP 工具

**Files**：
- Create：`scripts/ranked.py`（融合逻辑，fork 自定义面，只读直查 `.codegraph/codegraph.db`——与 adapter 同模式）
- Modify：`graphify/serve.py`（注册工具）
- Create：`tests/test_ranked_context.py`

**设计要点**：
- **混合查询路由（v1.7 补前置，Q8 裁决）**：路由规则为 **token 级分流**而非查询级二选一——identifier tokens 进 FTS 通道、CJK tokens 进图侧子串通道，双通道结果按 token 预算融合，`query_shape` 报告各通道命中数。验收用例"watch 防抖 降级"即此形态。
- **id 碰撞判定（v1.7 补，Q6 裁决）**：ranked.py `from adapter import _normalize_id` 复用同一 fold 函数（scripts 同目录，与 run_analysis 同模式），碰撞判定 = fold 结果含消歧后缀；不复刻逻辑（防两份实现漂移）、不做 sidecar。
- **token 计数降级（v1.7 补，Q2 裁决）**：tiktoken 是 optional extra（kimi/gemini/openai），非核心依赖——可用则精确计数，否则按 §8 的 ~4 bytes/token 估算（declared），`_meta` 报告计数方式。不升 core。
- **token 预算缺省 2000**（与 query_graph 一致，agent 认知惯性），可选参数暴露；金标集测试跑 1000/2000 两档确认命中@5 稳健。
- **四通道**：① 精确名 pinning——source-shaped token（限定名/CamelCase/snake_case）做符号名精确匹配（`nodes.lower_name` 索引在），上限 5 seeds，`_meta.query_shape` 报告；② FTS5 BM25 候选池（fork DB 实测事实）：
  - **建表语句原文**：`CREATE VIRTUAL TABLE nodes_fts USING fts5(id, name, qualified_name, docstring, signature, content='nodes', content_rowid='rowid')`——external content 表，FTS 不存数据、实时读 nodes 表；触发器同步**实测生效**（UPDATE 节点名后新名立即可搜）。
  - **join 铰链两条路**：文本 id join（`n.id = nodes_fts.id`，两侧均 text）与 rowid 直连（`n.rowid = nodes_fts.rowid`）均实测成立；ranked.py 走文本 id join（图侧节点 id = codegraph id 直传，adapter 已有碰撞测试 `test_id_fold_collision_remaps_edges`）。
  - **查询语法**：隐式 AND（`'watch debounce'`=1）、`OR`（34）、列过滤（`name:watch`=10，即只搜 name 列）、前缀（`'semantic*'`）全部可用；`bm25()` 越负越相关、`ORDER BY score` 升序即正确排序。
  - **列填充率（权重校准依据）**：name 100%、qualified_name 100%、**signature 92%、docstring 仅 5%（556/10219，且含 ASCII 艺术线等噪声）**——BM25 权重以 name/signature 为主力，docstring 低权重（sparse 且有噪）；`bm25(nodes_fts, w_id, w_name, w_qn, w_doc, w_sig)` 需传**满 5 个权重**（5 列）。
  - **CJK 路由**：unicode61 分词器下纯中文查询在 nodes_fts 必零命中（实测），trigram 不可改（上游 schema + 只读）。路径：identifier token → FTS；CJK token → 图侧语义节点（内存图 label/metadata 子串匹配，O(n)；语义节点规模 3,374，中文仅 34，召回面薄是诚实预期）。`query_shape` 显式报告 CJK 覆盖情况，杜绝把"没搜到"当"不存在"。
  - **辅助函数**：`highlight()` 可用（结果标注高亮）；`snippet()` 对 NULL 列返回 None（docstring 5% 填充下常见），实现需 COALESCE 兜底。
  ③ 结构通道——度数中心性，**合并图口径**（原始 DB 与折叠/remap 后度数不同）：候选 id 集 → 内存图取合并图度数；SQL GROUP BY 仅留作无图侧加载的降级路径（`_meta` 标注 `centrality=raw_db`）。graph.json 不缓存中心性分数（实测），不留档；④ token 预算装配——按预算截断，每条结果带 `stage` 与 `source_tool` 归因。
- **id 铰链**：FTS5 返回 codegraph id → 图节点 id。注意 adapter 的 `_normalize_id` 折叠碰撞会加消歧后缀，碰撞节点不参与 join（可接受损失，记录于 `_meta`）。
- **与 knowledge_gaps 联动**：低置信查询命中既有 gap 时返回 gap 提示而非空手。命中语义 = identifier token 与 gap 的 ref 符号名精确匹配（大小写不敏感）；命中返回 gap 摘要（符号名 + 关联文件 top3），`_meta.gap_hit = true`。模糊匹配明确不做（gap 本身是未解析引用，模糊放大噪声）。
- 响应复用 Track A 的 `_meta` 信封。

**验收**：
- [ ] graphify_fork 自测：查询"watch 防抖 降级"，top 命中 `watch.py` 相关符号。
- [ ] token 预算被尊重（tiktoken 计数 ≤ budget）。
- [ ] 20 查询金标集：每查询预写期望命中符号 id 集（2–5 个），随 `tests/fixtures/ranked_golden.json` 入库成永久回归；命中@5 = 期望集与 top-5 交集非空且覆盖率 ≥50%；前置工作（维护者本人 2–3h，**拆批**：10 条矩阵最小集 ~1h 先行跑通对照，10 条从 querylog 真实查询补齐——金标集只 gate 验收对照，实现与单元测试用合成 fixture 照常 TDD，两线并行）；融合排序 vs BM25-only 对照命中@5 不降。**构成**：矩阵覆盖（identifier 精确 / 混合 CJK / 图遍历型 3–5 个 / 低置信 gap 命中型）+ querylog 真实查询（升格语料提前用）；金标测试跑 1000/2000 两档预算。
- [ ] `_meta.query_shape` 正确报告 pinning 情况。

### B2 get_symbol_source 能力（get_node 扩展形态）

**Files**：
- Modify：`graphify/serve.py`（`_tool_get_node` 扩展参数 + 切片函数）
- Create：`tests/test_symbol_source.py`

**动机**：两步经济模型（search 元数据 → fetch 精确切片，不吐整文件）的取数侧空白——agent 检索到符号后仍需 Read 整文件定位。原料实测齐备：行区间覆盖 100%（10,692/10,692）、signature 92%、切片与 def 行吻合（三例验证）。

**设计裁决：融合而非新增工具**。get_node（单节点名片：label/id/source/type/community/degree）本就是 fetch 步，只还图属性不还源码。扩展优于新增：schema +~40 tok vs 新工具 +150–180（§8 预算纪律）；工具面不扩张（jCodeMunch 90+ 教训）；语义契合。代价 = 无批量取数（jCodeMunch 同为单符号，非关键损失）。

**设计要点**：
- **参数与缺省**：`get_node(label, include_source="none"|"signature"|"body"|"body+context")`，缺省 `signature`——fetch 步默认带源码（省 agent 一跳），典型 +20–50 tok；`none` 档为预算抑制出口 + 回归锁定锚点。`Code:` 段命名避开既有 `Source:` 行（位置信息）。
- **四档**：`none`（纯名片，等价扩展前输出）；`signature`（**缺省**，def 行 + signature + docstring 头，最省）；`body`（完整行区间切片）；`body+context`（±N 行 + 可选 1-hop 邻接签名摘要——超出 jCodeMunch 的图上下文附加值，切片自带调用方/被调用方线索）。
- **读时切片、不落盘**：按 id 直查 codegraph DB 行区间，查询时读文件现场切——零重复解析（codegraph 已承担 tree-sitter 的索引职能，无须第二套符号索引）、不违反只读章程。
- **名片增强（顺带）**：signature/docstring 从 DB 一次 join 带进 get_node 输出（现合并图侧无此二属性，检索体验直接提升）。
- **行漂移校验（核心边界）**：索引时行区间 vs 查询时文件状态在 rebuild 间隔内可能错位——切片后校验（name/signature 出现于切片头部），不匹配 → 文件内 fuzzy 重定位 + `_meta.slice_verified=false`；缺省档切片失败时名片仍返回（`Code:` 段省略并标注），不因切片失败毁掉整个响应；与 A1 freshness 信封联动（WAL mtime 判 stale 时预告漂移风险）。
- **search/fetch 不合并**：源码切片不塞进 ranked_context 结果，保持两步经济模型；B1 返回描述加一句指引“取实现用 get_node(include_source='body')”（缺省 signature 已覆盖看签名场景，body 才需显式）——行为引导写进工具描述，agent 自然走两步模式。
- **折叠碰撞**沿用 Q6 裁决：碰撞 id（含消歧后缀）不参与切片，记录 `_meta`。
- **语义节点分档处置**：docs 类节点无源码切片语义——缺省/none 档：名片正常返回、`Code:` 段自然省略（无成本）；显式 body/body+context 请求 → `absent`（Q9 三分规则：显式要源码而无源码可给）。
- **信封合规**：get_node 本属 A1 检索型清单（Q4）；include_source 切片校验失败 → verdict `low_confidence`。

**验收**：
- [ ] 无参调用返回名片 + `Code:`（signature）段；`include_source="none"` 输出与扩展前逐字节一致（回归锁定锚点移至 none 档）。
- [ ] `body` 切片与文件实际内容吻合（fork 自身符号抽样 ≥10 个）。
- [ ] 人为修改文件后（不改 rebuild），切片校验触发 fuzzy 重定位或 `slice_verified=false`。
- [ ] 语义节点请求 `include_source` → `absent`（Q9 三分规则路径正确）。

### B3 分发线索直查 + 动态分发追踪 + blast-radius（get_neighbors 扩展形态）

**Files**：
- Modify：`graphify/serve.py`（`_tool_get_neighbors` 扩展参数 + 分发线索直查 + fan-out 遍历）
- Modify：`scripts/adapter.py`（仅暴露 raw↔merged 边查证辅助函数，**不改既有映射口径**）
- Create：`tests/test_dispatch_trace.py`

**动机**：动态分发盲视——resolvedBy/confidence metadata 在 adapter 全部丢弃、上游把调用点坍缩为单目标（实测 0 多目标组）；blast-radius 工具面空白（get_pr_impact 是 PR 范围、get_neighbors 仅 1-hop、C3 方向相反）。实测（fork DB）：calls 16,215 条（exact-match 15,502 / instance-method 168 / fuzzy 15；**0.4 置信 4,915 条**）；跨语言边 68 条全为文件级 imports；extends 44 + implements 9 类层级边完整。

**架构裁决——分发线索 = 第三例直查，非 adapter 透传**（B1-FTS、B2-行区间之后的既定模式）。**分层原理**：单点事实查证（按 key 取值，无 hop）直查 DB；结构遍历（每个 hop 要求 id 衔接）留在合并图——merged→raw 反映射等价于重跑 adapter 折叠管线，中间形态不存在。合并图不灌 0.4 置信标签、§3.2 口径不动。

**设计要点**：
- **get_neighbors 扩展**（blast-radius 载体）：`get_neighbors(label, direction="out"|"in"|"both", depth=1..3, edge_kinds=[...])`——direction="in" + depth=N 即反向 BFS blast-radius；缺省参数与现状完全一致（Q12 纪律）。schema 估 +30–50 tok。
- **top-k 截断（防 token 放大器）**：中心节点反向闭包可达数百节点——返回按度数排序的 top-k（k 缺省 50）+ 截断标志，闭包全量尺寸计入 `_meta`。
- **分发线索直查**：邻接边输出时按边 id 直查 codegraph DB edges 表 metadata，附 `resolvedBy`/`confidence`。单点查证、无 hop、符合直查分层原理。**dispatch_candidate 判定（双判据并集）**：`resolvedBy ∈ {instance-method, fuzzy, qualified-name, None}` **或** `confidence < 0.9` 任一命中即 true（0.4 是明确猜测、0.7/0.85 也是推断——advisory 体系下宁多标勿漏标；阈值调整须过金标对照）。
- **合并边归属（最保守取值）**：合并边（fold 多对一）的 confidence = 组内 raw 边最小值、dispatch_candidate = 组内任一命中即 true（取均值/首条会把“组内存在猜测边”洗白）；resolvedBy 可为数组展示全部。
- **fan-out 重展开（合并图独有路径）**：对 dispatch_candidate 边，查询期三边联走——call 边目标方法所属类（反向 contains 边，class→method 384 条实测成立）→ extends/implements 反向遍历得子类集 → 各子类 contains 下同名方法 override = 全部可能目标。fork 侧零上游依赖。**起点无所属类时降级不展开**（实测 16 条 method 直接被 file contains——file 范围同名匹配误报面大）→ 返回原单目标 + `fanout=unavailable: no owning class` 标注。
- **跨语言维度随索引自动伸缩**：adapter 按边 kind 映射语言无关，合并图遍历不看语言——codegraph 索引到的跨语言边自动参与反向可达，fork 侧零额外工作（当前 68 条全为 fixture 文件级 imports，真实价值待多语言项目被索引后兑现，不预支成本）。
- **Q14 前置继承**：输出建立在边语义不完整的图上（C1 实测根因共享），缺省 verdict `low_confidence`、advisory 定性；fan-out 展开放大边语义误差（三种遍历各自带误差），advisory 不可省。
- **信封合规**：get_neighbors 本属 A1 检索型清单（Q4/Q18）；分发候选/低置信邻接 → verdict `low_confidence`。

**验收**：
- [ ] 缺省参数输出与扩展前一致（回归锁定）。
- [ ] `direction="in", depth=2` 返回 2-hop 反向可达集，含 top-k 截断标志与 `_meta` 全量尺寸。
- [ ] 邻接边含 `resolvedBy`/`confidence`（抽样与 DB 直查吻合）。
- [ ] dispatch_candidate 边的 fan-out 展开覆盖所有子类 override（构造 fixture：基类 + 2 子类同签名）。
- [ ] 输出 verdict `low_confidence` + advisory 措辞（Q14）。

---

## 5. Track C — 分析层结构查询（含 git 轴）

### 动机

结构查询四件套（dead code / untested / hotspots / changed_symbols）数据现成、算法空白：前三类在合并图上就是 BFS；C3 让重建报告"这次图里变了什么"。当前零 git 集成——C3/C4 构成新能力轴。

### C1 find_dead_code

**Files**：Create `scripts/structure_queries.py`（纯函数，输入 networkx 图，fork 自定义面）；Modify `graphify/serve.py`（注册 MCP 工具，吃 A1 信封）。run_analysis CLI 挂载降级为调试入口（非交付物）。

**交付形态（v1.11 定夺）**：C 轨道全部为 **MCP 工具**（agent 按需查询——价值场景是 agent 提问“哪些符号疑似 dead”，rebuild 时刻静态报告无提问方），与 C 信封纪律（serve.py 注册 + 强制信封）一致。

**设计要点**：入口集合可配置（main 脚本/CLI 入口/setup.py/公开导出模块）→ 正向可达闭包 → 不可达符号报告。**可识别性已实测**（fork 自身 DB）：`name IN ('main','__main__')` 命中 12 个、`setup/cli/run/app` 命中 7 个——入口锚点充足；kind 分布以 function 6,787 / import 1,546 / file 401 为主，闭包遍历规模可行。动态分发是静态图的天生盲区——结果一律 advisory，配 verdict `low_confidence` 分级，不声称确定性。另：`unresolved_refs` failed 实测 32,354 条（Knowledge Gaps 输入规模，adapter 的 top100 截断决策因此正确）。

**实测设计约束（原型 86.4% 误报的教训：库型仓库符号经 re-export 暴露、不经 main() 触达，且边语义不完整）**：① 入口分项目类型——应用型用入口闭包，**库型以公开导出面为入口**（`__init__.py` 的 `__all__`/re-export 边/setup.py entry_points），入口配置项选择，缺省自动探测（有无 `__main__.py`）；② 遍历边集合含 import 推导（import node 视作使用证据）；③ 验收闸门——库型入口修正后不可达率仍 >50% 即设计失败，降级为孤儿符号提示。
  **② 前置条件标注**：边属性 `type_only` 只存在于 graphify 原生 AST 管线、不存在于合并图（adapter 仅透传 synthesizedBy）——本条款仅当“codegraph 记录 type-only + adapter 透传”落地后生效；生效语义：不算运行时可达证据（编译期擦除）、算结构引用证据，可达闭包排除、孤儿判定计入引用侧。动工时核 codegraph schema 再议（触碰 §3.2 须单独裁决）。

**验收**：graphify_fork 上运行，手工白名单核对（已知动态调用符号不误报，或明确分级列出）；>50% 闸门（库型入口修正后不可达率仍超即设计失败，降级为孤儿符号提示）。

### C2 get_untested_symbols

**Files**：同 C1（追加函数）。

**前置与闸门**：边语义不完整是 C1/C2 共享根因（import 了 X 但调用边缺失 → X 误判 untested）——① C1 入口修正先落地，C2 排其后；② 实测闸门——抽样 20 个 untested 符号人工核对，误报率 >30% 降级为“疑似未覆盖（advisory）”。**设计要点**：测试文件判定（`test_*.py`/`*_test.go`/`*.spec.ts` 约定）→ 测试子图正向可达 → 未覆盖 = 全集 − 可达集。Python 单约定可启动（实测 `test_*` 命中 297 文件），go/ts 留配置。同样 advisory。

**验收**：graphify_fork 上，已知被测试覆盖的符号不在结果中。

### C3 get_changed_symbols（git 轴，本轨道最高价值）

**Files**：Create `scripts/git_symbols.py`；Modify `scripts/rebuild_entry.py`（完成后可选附加报告）。

**设计要点**：`git diff --name-only` → 文件集 → 图中 `source_file ∈ 文件集` 的符号集。**基线锚定**：rebuild 成功时在 A1 状态文件记 `git_head`，下次用 `git diff <上次hash>..HEAD` ∪ 工作区 diff。**graph_diff 回退（v1.14 实现偏差，语义变更）**：首次运行（无记录）/ 孤儿 hash / 非 git 时——原方案"回退 graph_diff 图内对比"不可执行（run_analysis.diff 需前置图快照，首次/孤儿无从对比；naive 文件集对比 fork 实测假阳性 6 个），实现改为**诚实空标记**：`basis="graph_diff"` + 空结果 + serve 判 absent（"没有变更信息"≠ok）。集成点：rebuild_entry 成功路径末尾附加变更摘要（进当时活跃会话的上下文——外部变更感知职责归属，A4 内容设计依赖此边界）。**边界**：(a) amend/rebase 后孤儿 hash → `_git` 返回 None → 回退诚实空 + stderr 告警；(b) 非 git 仓库/git 不在 PATH → 整体 no-op + 一行日志。git 集成全收 scripts/，graphify 包零触碰（R14）。

**验收**：改一个文件触发 rebuild，changed_symbols 输出与 `git diff --stat` 文件级吻合；符号级命中该文件内的节点。

**C 轨道工具信封纪律**：所有新增工具强制携带 Track A 信封，verdict 缺省映射——dead_code/untested → `low_confidence`（边语义根因）、changed_symbols/hotspots → `ok` + `confidence=declared`。与 A1 检索型清单同源维护，不进清单的实现拒绝合并。

### C4 get_hotspots

**Files**：同 C3（追加函数）。

**设计要点**：churn（文件 commit 频次，依赖 C3 的 git 轴）× 复杂度代理（节点度数——edges 表一次 GROUP BY 可得；**实测 graph.json 无文件行数属性，"文件行数"需读磁盘，默认不用**。标注 declared，我们不假装有圈复杂度数据）。排序输出 top-N。

**验收**：graphify_fork 上输出合理（高频改动文件的高连接节点靠前）。

---

## 5.5 Track B 运维项 — B4 cache GC（独立可先行）

**产物重叠核查结论（2026-08-31 实测）**：graphify-out 全部疑似重复项均有正当读取方——`.graphify_analysis.json` 被 lessons 新鲜度检查消费（cli.py）、`GRAPH_REPORT.md` 是 agent 指引的一等公民 + callflow_html 输入、knowledge-gaps 是 DB top100 截断摘要（R13 登记正确）——**无可合并项**；一次性遗留清理项（`graph.json.bak-pre-merge` 15.9MB、5 个空日期目录）由维护者手动处理，非方案任务。

**问题**：`cache/ast/` 两条无界增长轴——①键=内容 hash，文件每改一次旧版本条目永久滞留（实测 M2 时 934 条，测试隔离修复后正主 103 条）；②版本目录随 cache schema 升级累积，旧版本目录永不清理。`cache/semantic/`（160+ 条）同病。

**方案：manifest 锚定的 mark-and-sweep GC**

```
live 集 = manifest.json 全部 ast_hash ∪ semantic_hash 值（现成账本，零新状态）
sweep  = cache/ast/<ver>/ 与 cache/semantic/ 中 键∉live 集 且 mtime 早于 7 天宽限窗 的条目
       + 非 cache.py 当前版本常量的整版本目录
```

**设计要点**（七条）：
1. **挂载点**：rebuild_entry 成功路径末尾（状态文件更新同区），复用 mkdir 锁——GC 在锁内执行，与写权单一化纪律同构，零新锁。
2. **频率门控**：条目数超过 `live数×2 + 64` 阈值才触发 sweep——摊销扫描成本，不是每次 rebuild 都扫。
3. **7 天宽限窗**：保护 git checkout/回退场景（文件内容还原 → 旧 hash 重新变 live；误删只损失重提取，宽限窗把这个损失也消掉）。
4. **安全性由构造保证**：cache 按内容 hash 键 → 删任何条目最坏代价 = 下次 rebuild 重提取，**永不影响正确性**。
5. **semantic cache 顺带治理**：manifest 的 `semantic_hash` 字段白送——同一套 sweep 覆盖 `cache/semantic/`。
6. **遥测**：一行日志 `cache gc: swept N orphans / M live / K bytes freed`，不进状态文件（避免 schema 膨胀）。
7. **零参数面**：宽限窗/阈值全部模块常量，不加 CLI flag（工具面纪律）。

**Files**：Modify `scripts/rebuild_entry.py`（锁内 GC 调用）；Create `scripts/cache_gc.py`（纯函数）+ `tests/test_cache_gc.py`（造 live/orphan/宽限窗内/旧版本目录四类 fixture，断言只删该删的）。

**验收**：① 修改文件若干次触发 rebuild → orphan 条目在宽限窗过后被 sweep、live 全存活；② manifest 中所有 hash 对应条目 100% 保留；③ GC 日志一行含数字；④ 频率门控——条目数未超阈值时 sweep 不执行。

**估**：~60–80 行 + 测试，0.5 天；与 A–E 轨道零耦合，**可立即独立先行**（不依赖 A1 信封定形）。

---

## 6. Track D — 跨模态双层图（原 Phase 4 保留项）

原文保留：LLM 语义遍历覆盖 docs/ADR/PDF，与符号图合并为双层图（跨模态加权已内建）。

**本方案补充两点**：
1. **载体已就绪**：Phase 1.5 的种子机制（`split_semantic_seed.py` + run_analysis 的 `semantic_refresh` 增量路径）正是跨模态节点的注入通道——本轨道不需要新管道，需要的是 LLM 遍历的质量控制（成本预算、输出确定性）。
2. **评级沿用原文**：难度 ★★★★★、收益 ★★★☆☆、不在本期承诺范围。jCodeMunch 在此维度无可吸收项（它无 LLM/多模态层）。

双向写回否决决策保留（见 §11）。

---

## 7. Track E — 押后项：SCIP 证据导入

jCodeMunch 的 `import-scip` 摄入编译器索引，使 `find_references` 拿到 confidence 1.0 的编译期证据，且"证据只导入、从不执行"保持只读章程（[UNDER_THE_HOOD Ch.6](https://github.com/jgravelle/jcodemunch-mcp/blob/main/UNDER_THE_HOOD.md)）——后者恰好印证我们 adapter 只读桥接的设计。

**押后理由**：需要 CI 产出 SCIP 索引（scip-typescript 等）+ 适配器旁挂第二 DB + 双证据合并逻辑，成本 ★★★★★；收益集中于 TS 项目（我们主力是 Python）。触发条件：若 Phase 4 前四轨完成后主力项目出现 TS 比重显著上升，再评估。

---

## 8. 计量纪律与效率基准（跨轨道）

jCodeMunch 三条工程纪律整体移植（[UNDER_THE_HOOD Ch.3–5](https://github.com/jgravelle/jcodemunch-mcp/blob/main/UNDER_THE_HOOD.md)）：

1. **每个数字声明依据**：confidence/效率数字标 `measured`（可复现工件与实测一致）或 `declared`（工程先验）；断言落点为 pytest（本仓无 CI）——`tests/test_schema_budget.py` 枚举 list_tools schema 计数、measured 工件一致性测试读 `benchmarks/` 重算比对；check-custom.sh 登记测试文件名防上游合并丢弃。`declared` 金标语料足够后才可升格。
2. **计量只向下误差**：效率基准按 ~4 bytes/token 折算（低估密集代码）；空/负结果计零；命中文件每次调用只计一次；token 计数而非金额。
3. **工具 schema 预算**：serve.py core 层 schema 总量 ≤ 4000 tok（pytest 断言），以纪律代替 90+ 工具面教训。**基线**：10 个工具（query_graph/get_node/get_neighbors/get_community/god_nodes/graph_stats/shortest_path/list_prs/get_pr_impact/triage_prs）合计 769 tok（tiktoken 实测）；Track B 增量估：ranked_context +150–180（declared）、B2 +40、B3 +30–50——合计 ~1000 tok，余量充足。

**工具面治理（三条裁决）**：
1. **PR 三件套收敛——描述引导先行，物理合并缓议**：triage_prs 覆盖 list_prs + get_pr_impact，可 3→1 省 ~100–150 tok；但三者是 upstream 代码，删除是 fork diff 且背合并负担（R14 反向）。**零 diff 先行**：工具描述使用引导（“优先 triage_prs，list_prs 仅当需要含 stale 全量、get_pr_impact 仅当深挖单 PR”），querylog 观察调用分布，triage 主导模式稳定后再议物理合并。
2. **query_graph × B1 重叠——用证据裁决，不预先砍**：两者都是"问题→相关节点"但语义不同（query_graph 沿边遍历 bfs/dfs，B1 是 FTS×结构融合检索）。裁决：B1 金标集（Q17）刻意混入 3–5 个图遍历型问题（"X 经过哪些中间步骤调用到 Y"类），用对照数据决定 query_graph 是退役还是保留为图导航专用。
3. **概览/导航类不精简**：god_nodes/graph_stats/get_community/shortest_path schema 共 ~200 tok、职责唯一——砍了省不到 150 tok，损失图导航能力，不划算。**get_neighbors 除外（v1.9）**：B3 扩展后语义升级为“邻接 + blast-radius + 分发追踪载体”（direction/depth/edge_kinds），扩展前述“职责唯一”判断不再适用——但形态仍是参数扩展非新工具，§8 预算纪律不受冲击。

**参数类型统一条款**：受限取值参数（include_source 四档 / B1 token_budget / B3 direction 等）schema 用 **enum**，无效值走参数校验失败 → isError 路径（符合 A1 isError 边界：参数错误是真错误），不静默回退缺省。

**效率基准任务**（随 Track B 落地）：固定 10–15 个真实查询任务集，pinned commit，对照 (a) graphify+codegraph 合并栈 (b) 原生 read/grep 工作流，tiktoken 计数，脚本与结果存 `benchmarks/`。**对照协议**：① 同一起跑线——基准脚本先跑 rebuild，紧接两栈各答一轮（合并栈 freshness=fresh，read/grep 读同一 commit 工作区）；② 正确答案复用 B1 金标集期望符号集（同一份人工标注服务两处）；③ 只量 token 与命中@5，不量耗时（环境噪声大，§8 只向下误差原则下不值得纳入）。这是"合并栈到底值多少"的价值证明，也是对外（上游 PR、团队推广）的论据。

---

## 9. 前置条件与依赖

Phase 0–3（Task 1–16）已全部完成合入（feature 26 commits → `v8-custom`，主仓 `5f600dd` 合入 upstream/v8 后实测 22 用例无合并损伤）。**全部轨道无阻塞**：A1–A4/B/C1–C4/D 的依赖（rebuild_entry finally 块、call_tool 统一出口、adapter、precompact hook、codegraph DB FTS5、合并图链路、种子机制）均已就绪。B 依赖 A1 `_meta` 字段定形；C2 依赖 C1 入口修正（Q14）；其余可并行。

---

## 10. 风险登记册（增补 R12–R17）

| # | 风险 | 等级 | 对策 |
|---|---|---|---|
| R12 | verdict/confidence 伪精度（declared 冒充 measured） | 中 | §8 出处纪律为 Track A 验收项；CI 断言工件 |
| R13 | codegraph FTS5 表结构漂移破坏 ranked.py | 低（表结构已实测：`nodes_fts` external content + 触发器同步） | 与 adapter 同样的版本门控；月度 diff 上游 schema |
| R14 | git 轴扩大 fork 自定义面，增加上游合并冲突 | 中 | git 集成全收 scripts/，graphify 包零触碰（沿用 §5.1 吸收模型） |
| R15 | dead_code 误报（原型实测：仅 main 入口的闭包 86.4% 不可达，抽检全误报） | **高**（不做入口扩展时）/ 中（入口策略修正后） | C1 设计修正已入正文：库型项目以公开导出面为入口 + import 推导 + 86.4% 入验收基准（修正后 >50% 即设计失败） |
| R16 | 脱敏正则误伤正常内容 | 低（**实测 775 文件 0 命中 0 误伤**） | 两级分层：L1 前缀类三重校验；L2 泛型默认关逐条校准；只在响应出口不改数据；固定误伤样例集回归 |
| R17 | A4 注入通道依赖客户端 hook 协议细节 | 低（v1.13 已消除二选一赌注：注入走 SessionStart + matcher:compact——官方文档确定通道；快照文件为主机制，PreCompact 只落盘不注入） | 实现时本机实测注入效果（压缩首问可引用快照）；PostCompact 注入能力文档未明确，留作优化项非交付物 |
| R18 | 纯中文查询在 FTS 通道静默零命中 | 中（实测确认；另实测图侧中文语义节点仅 34/13,936，召回面薄） | query_shape 显式报告 CJK 覆盖情况；纯中文查询路由图侧语义层；杜绝把"没搜到"当"不存在"返回 |
| R19 | cache/ast（及 cache/semantic）无界增长——内容 hash 旧版本条目 + 版本目录双轴累积 | 低（当前 103 条不痛，慢性膨胀） | **已实现（v1.14 状态更新）**：B4 manifest 锚定 GC 落地——live 重算锚定（`file_hash` 复用 cache.py 公开函数对 manifest 文件清单重算，落 cache key 域，零上游触碰）+ AST/semantic 双轴 sweep + 7 天宽限 + 频率门控；re-review 实测锚定 40/40 命中 |

---

## 11. 非目标（明确不做）

1. **MUNCH 二进制线格式**：中位省 45.5% 字节但需客户端专用解码器，破坏通用 MCP 互操作——JSON 级压缩（截断/字段裁剪）拿一半收益即可。
2. **工具面扩张至 90+**：以 §8 的 schema 预算纪律代替；Track A–C 新增工具数控制在个位数。
3. **遥测计数器 / 商业授权体系**：与本项目无关。
4. **双向写回 codegraph**：沿用 v4.2 否决决策——侵入 schema 与迁移体系，收益不明确。
5. **引入 jCodeMunch 依赖**：零依赖原则，机制自建。

---

## 12. 附录：吸收项 → jCodeMunch 出处索引

| 吸收项 | 出处 |
|---|---|
| verdict 契约 / freshness / 校准 confidence | UNDER_THE_HOOD Ch.1 |
| 通道融合 / 精确名 pinning / regret loop | UNDER_THE_HOOD Ch.2 |
| measured/declared 纪律 | UNDER_THE_HOOD Ch.3 |
| 计量只向下误差 | UNDER_THE_HOOD Ch.4 |
| schema token 预算 / 工具分级 | UNDER_THE_HOOD Ch.5 |
| SCIP 导入（押后） | UNDER_THE_HOOD Ch.6 |
| 结构查询清单（dead/untested/hotspots/changed_symbols） | CAPABILITIES.md |
| 响应脱敏 | CAPABILITIES.md |
| PreCompact 快照 / Read Guard 模式参考 | AGENT_HOOKS.md |
| 效率基准方法论（pinned commit / A/B） | README.md Evidence 节 |
