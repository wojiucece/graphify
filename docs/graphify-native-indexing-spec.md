***

label: ready-for-agent
source: wayfinder map（docs/wayfinder/MAP.md，7 票决议：01 提取盘点 / 02 直查点盘点 / 03 字段契约 / 04 watcher 架构 / 05 FTS 缓存 / 06 工具迁移 / 07 收尾）
date: 2026-09-02
----------------

# Spec — graphify 原生索引能力（codegraph 运行时退役）

## Problem Statement

graphify fork 当前依赖 codegraph（TypeScript 运行时）提供符号事实层：持久化数据库、FTS5 全文检索、文件监听自动同步、后台守护进程。这带来四个问题：

1. **双运行时负担**：Python 之外维护一条 node 进程链（watcher/daemon/sync），Windows 下进程管理与排障成本高。
2. **坐标系割裂**：符号数据在 codegraph.db，分析与语义在 graph.json，两者经适配器单向流动——每条链路（serve 查询、ranked 检索、快照、重建编排）都要跨坐标系 JOIN。
3. **检索面残缺**：graphify 自身产物不含 signature/docstring（实测 11606 节点 0 覆盖），检索深度受制于 codegraph 的 schema 与生命周期。
4. **能力不可持续**：codegraph 演进（WAL、schema 迁移、TS 解析器元数据）全部是外部不可控变量，fork 每月都要消化。

## Solution

退役 codegraph TS 运行时，其四大优点在 graphify 的 Python 自定义层原生重建：graph.json 与 extract 产物是**唯一事实层**（不建持久事实源数据库），FTS5 检索由**落盘可重建缓存**（sqlite，删除无损）承载，自动同步由 **serve 进程内置 watcher** 承载，全程保持较低的自定义维护成本（新能力集中在新增文件，上游补丁面最小化）。

graphify 的图语义原样保留：无向 networkx.Graph、边属性标方向、自由 relation 词表、强制三档置信度（EXTRACTED/INFERRED/AMBIGUOUS）。

## User Stories

**提取深度**

1. As a 代码库开发者, I want 每个函数/方法节点携带参数与返回类型签名, so that 不读源文件就能判断调用形态。
2. As a 代码库开发者, I want 符号节点携带 docstring 原文（上限 1500、智能截断、量化超限标记）, so that 检索与浏览时直接看到文档。
3. As a 代码库开发者, I want 每个符号有可读的限定名（Class::method 作用域链）, so that 同名符号不再混淆。
4. As a 代码库开发者, I want 符号定位精确到行列起点与结束行/字节, so that 源码切片不再猜测块尾。
5. As a 代码库开发者, I want 构造器签名与类关联可检索, so that `UserService(name: str)` 这类搜索能命中。

**检索（FTS5）**

1. As an AI 编码助手, I want 用自然语言搜符号名/docstring/签名并拿到 BM25 排序结果, so that 不用 grep 整个仓库。
2. As an AI 编码助手, I want camelCase 标识符按语义分段命中（pinningSearch ↔ "pinning search"）, so that TS 代码检索不漏。
3. As an AI 编码助手, I want 概念节点（"attention mechanism"这类语义概念）进检索并有权重加成, so that 跨代码与文档的知识面可搜。
4. As a 代码库开发者, I want 检索缓存删了能自动重建, so that 缓存永远不是新的故障源。

**自动同步（watcher）**

1. As a 代码库开发者, I want 文件保存后索引秒级自动更新, so that 查询永远命中最新代码。
2. As a 代码库开发者, I want 防抖与失败退避（批量合并、重试上限、指数退避）, so that 高频保存不打爆重建。
3. As a 代码库开发者, I want 文件删除/重命名后亡灵节点消失, so that 图不积累已删代码。
4. As a 代码库开发者, I want 退出时正在写的批次安全落盘, so that graph.json 永远完整。
5. As a 代码库开发者, I want 无 watchdog 库时自动降级为 mtime 轮询, so that 零新硬依赖。

**工具面（MCP / CLI）**

1. As an AI 编码助手, I want get\_node 名片四档（签名/源码切片等）在新链路上语义不变, so that 现有工作流无感知迁移。
2. As an AI 编码助手, I want ranked\_context 的多通道融合排序与旧链路等价（95% 金标闸门）, so that 检索质量不回退。
3. As an AI 编码助手, I want 动态分发信号由边置信度与解析方式属性承载, so that 多态 fanout 判断能力保留。
4. As an AI 编码助手, I want 失败引用（knowledge gaps）仍可检索与联动, so that "哪些引用没解析"的问答不消失。
5. As an AI 编码助手, I want freshness 判定继续工作（缓存 vs 事实层时间戳）, so that 陈旧索引被诚实标注。

**维护与收尾**

1. As a graphify 维护者, I want 新能力集中在新文件、上游补丁最小化, so that 月度上游合并仍是小时级成本。
2. As a graphify 维护者, I want 测试一次性换源不留双 fixture, so that 测试面不长期双轨。
3. As a graphify 维护者, I want 首次新链路 rebuild 后直接落新基准, so that 后续变化有可比对的基线。
4. As a 代码库开发者, I want codegraph 运行时与 vendored 副本最终退役, so that 仓库只留一条 Python 链路。

## Implementation Decisions

> 全部细节（含精确改动点坐标、行数估计、参考实现指针）在各票 Resolution 内，本 spec 只收口决策。术语见根目录 CONTEXT.md。

### 架构总则

- 单一事实层：graph.json 与 extract 产物；**不建持久事实源数据库**（sqlite 仅作可重建缓存）。

- 数据流单向：文件 → extract（per-file cache 增量）→ build → graph.json → FTS 缓存投影 → 查询面。

- codegraph TS 运行时退役；vendored v1.5.0 副本仅作实现参考（吸收其设计，不抄代码；MIT 署名在吸收处保留）。

- 实施顺序：提取契约 → FTS 缓存 → 工具迁移 → watcher → 收尾（见 Further Notes）。

### 提取契约（票 03 + 票 06 Q1/Q2）

- 节点新增顶层字段六件套：`signature`（按 kind 语义化，名字不进签名；变量存 `=` 右侧头部、按 token 边界截断 100、截断必加标记；构造器单独抽取并与类关联）、`docstring`（原文、上限 1500、\[1350,1500] 窗口内取**最靠后**的段落>行>句边界断点——窗口内越靠后保留内容越多，段落界若靠后仍优先于靠前的句界，窗口内无任何边界才硬切 1500、`…[+N chars truncated]` 量化标记、<5 字符存 null）、`qualified_name`（`Class::method` 链；模块级无前缀、局部函数不进链、与 nid 并存）、`source_location` 升级 `L110:C5`（文件节点豁免保持 L1、边不加列）、`end_line`、`end_byte`。

- 首批语言覆盖：engine 通用提取层的 15 个 LanguageConfig + docstring 仅 Python/JS/TS；其余语言与 26 个专用提取器押后（FTS 实战验证检索价值后再扩）。

- 边新增 `resolved_by` 属性（各语言 resolver 按解析路径打点）：

```
static-type     → "qualified-name"
instance-method → "instance-method"
heuristic       → "fuzzy"
（runtime-profiled 无源，消失）
```

- **dispatch 作为独立概念退役**：语义被边属性 `confidence`（EXTRACTED 不标 / INFERRED / AMBIGUOUS 保留）+ `resolved_by` 吸收；`_edge_dispatch_info` 双副本与金标删除；工具 description 诚实标注语义变化。

- raw\_calls 跨文件解析循环内新增失败收集器（`{from_node, callee_name, line, file_path}`），作为 unresolved\_refs 的 AST 等价物，供 knowledge gaps 与 gap\_hit 通道换源。

- 落点纪律：字段走节点/边顶层属性（build 的 add\_node 无白名单透传）；不走 metadata（会被 sanitize 截断转义）；rationale 节点保留不动。

- **模块（文件）节点的 docstring 字段是 Task 01 文件节点豁免的显式例外**——docstring 落文件节点（模块即文件），其余五件套（signature/qualified\_name/L:C/end\_line/end\_byte）不落。

### FTS 缓存（票 05）

- `graphify-out/.fts-index.db`：三表——nodes 元数据表（全量节点，服务点查类工具）、nodes\_fts FTS5 外部内容表（content='nodes'，5 列 id/name/qualified\_name/docstring/signature）、graph.json (mtime\_ns, size) 指纹表。

- bm25 权重 `(0, 3, 2, 0.2, 1)` 与 tokenizer（默认 unicode61）逐字对齐旧链路，保证排序等价迁移。

- camelCase 分段：索引与查询双侧预拆（camel 边界/下划线/数字边界 → 空格），共用同一拆分函数。

- 索引范围：AST 符号 + 语义概念节点（label 双列同值 = 权重 5 加成；docstring 列空串起步，接口留待 description 字段出现）；file 节点不进 FTS。

- 过滤路径照搬旧链路：元数据表 WHERE 先收窄（kind/source\_file）再 JOIN FTS。

- 构建原子替换（tmp + rename）；外部内容表的手动同步命令留接口（首期全量 rebuild，未来局部更新的口子）。

- 失效两级：watcher 进程内直通；serve 重启指纹对比惰性重建。

### Watcher（票 04）

- 新文件承载全部逻辑（\~250–350 行），serve 挂载点 diff ≤ 25 行；默认关，SessionStart hook 拉起时显式开。

- watchdog 软依赖：import 失败降级 mtime 轮询（5s 间隔、scandir 非递归栈、轮询发现变化直接进常规防抖窗）。

- 防抖/退避常量复用 watch.py 已移植的 codegraph 算法（快窗 300ms、重试上限 5、退避上限 30s、分批上限 500）。

- 触发链：文件变化 → 防抖聚合 → extract 增量（含删除处理）→ 全量 build → graph.json 原子落盘 → FTS 重投影 → serve 缓存进程内直通失效原子换图。

- 三条铁律：删除语义（pending 删除集 + extraction 剔除 + MovedEvent 拆 delete/create + 按文件系统实况兜底过滤，防亡灵节点）；graceful shutdown（stop() 阻塞等待当前批次完成，不许只发信号）；重建期间查询走旧图、完成后原子换。

### 工具面迁移（票 06）

- 10 个纯图工具零迁移。

- get\_node：元数据点查换 .fts-index.db；`__cg` 消歧后缀与回退点查删除（id 原生 kind+hash 形态）；源码切片从行范围升级 end\_byte 字节切。

- get\_neighbors / blast\_radius：dispatch 标注由边属性承载（见提取契约），响应不再有 dispatch\_candidate 字段。

- get\_ranked\_context：检索换 .fts-index.db（schema 与权重逐字平移）；gap\_hit 通道换 raw\_calls 收集器源保留。

- get\_changed\_symbols / get\_hotspots：符号查询换 graph.json 内存索引（按 source\_file 建索引）或 FTS 缓存元数据表；度数单位差异诚实标注。

- freshness：WAL mtime 对比 → FTS 缓存 mtime vs graph.json mtime。

- scripts 重组：run\_analysis 改 analysis-only（graph.json 直读，adapter 依赖删除）；rebuild\_entry 保留锁/stale 接管壳、内部换"extract → build → to\_json → rebuild\_fts"编排、指纹字段换 graph.json 时间戳（状态文件 schema v2）；hooks 不动；pyproject 的 codegraph 相关依赖评估移除。

## Testing Decisions

- 好测试的标准：只断言外部行为（产物字段形态、查询结果、工具响应），不断言实现细节；诚实性标注（截断标记、语义变化、单位差异）本身是被测行为。

- 四层 seam，全部复用现有测试先例，零新 seam：

| Seam     | 验收面                                                        | 先例                       |
| -------- | ---------------------------------------------------------- | ------------------------ |
| 1 提取产物契约 | extraction dict 六字段 + resolved\_by + 失败收集器断言（fixture 文件驱动） | 提取测试的 fixture 模式         |
| 2 产物对    | graph.json 节点字段 + rebuild\_fts 构建与查询行为（快照 + bm25 断言）       | adapter 快照测试模式           |
| 3 工具/查询面 | serve 工具响应、ranked 融合金标 **95% 阈值**（5% 容差给上游提取覆盖差异）、bm25 等价性 | serve 与 ranked golden 测试 |
| 4 触发链    | watcher 端到端：文件变化 → 产物更新 → 热重载（含删除/重命名/优雅停机用例）              | watch 重建触发测试             |

- 测试数据一次性换源：codegraph fixture 替换为 mini graph.json + rebuild\_fts 产物，测试逻辑不变只换数据源；金标在提取契约落地后重建；不留双 fixture 中间态。

- GRAPH\_REPORT 与 efficiency benchmark：首次新链路 rebuild 后直接替换基线提交，不做 diff 对比（kind 词表/边集/gaps 源全变，不可比）。

## Out of Scope

- 持久化事实源数据库（已裁决不建；FTS 缓存定性为可重建派生物）。

- graph.json 消除（phase5 v1.1 方向，已作废）。

- codegraph 运行时保留路线（已裁决退役）。

- SCIP 编译器证据导入（维持押后）。

- 上游 PR 回传终裁（实施完成后评估；基调是本地先行、留路不封死）。

- 提取增强的语言扩展（Go/Rust docstring、26 个专用提取器）——等 FTS 实战验证检索价值。

- 独立路径的图级增量合并逻辑迁移（增量 = extract cache + 全量 rebuild，已定夺）。

## Further Notes

- **实施顺序与依赖**：提取契约（含 resolved\_by 打点与失败收集器，是其余一切的地基）→ FTS 缓存 → 工具迁移（含测试换源与基准重落）→ watcher（触发链串联，可与工具迁移后半并行）→ 收尾票 07（.codegraph/ 退役、vendored 副本处置、6 个存量项目迁移、文档修订与退役 ADR、基准提交）。

- **精确坐标**：每个决策的改动点（文件:行、改动量估计、下游兼容点清单）在 wayfinder 票 01–07 的 Resolution 内，实施时以票为准。

- **参考实现**：vendored codegraph v1.5.0（提取器钩子模式 / FTS schema / watcher 常量）在收尾票删除前是唯一参考窗口，实施期间不要提前删除。

- **上游合并纪律**：新能力集中新文件（watcher、FTS 模块）；engine/serve 的必要改动保持参数化与集中（helper + 构造点），为未来 PR 留路；check-custom.sh 登记全部新增文件。

- **验收总口径**：新链路全绿 = 四层 seam 测试通过 + 金标 95% + benchmark 基线重落 + 首次 rebuild 的 GRAPH\_REPORT 提交为新基线。

