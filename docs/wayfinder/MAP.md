# Wayfinder 地图 — graphify 原生索引能力（codegraph 优点吸收）

label: wayfinder:map
status: closed

> 2026-09-04 实施完成（票 01–11 全部落档）。退役 ADR：`docs/adr/0001-retire-codegraph-runtime.md`；
> 各票 Resolution 在 `docs/issues/01–11`；新基线：`benchmarks/results-2026-09-04.json`。

## Destination

退役 codegraph TS 运行时依赖，其四大优点（自动同步、FTS5 全文检索、后台守护进程、随时查询）在 graphify fork 内原生重建；graph.json/extraction 为唯一事实层（不建持久 DB），graphify 图语义与产物管线保留；全程保持较低的自定义维护成本。

## Notes

- **实施 spec 已发布**：`docs/graphify-native-indexing-spec.md`（label: ready-for-agent，to-spec 综合 7 票决议产出；四层测试 seam 已经用户确认）。实施以 spec 为入口、票 Resolution 为精确坐标。
- **实施票集已发布**：`.scratch/native-indexing/issues/01–11`（to-tickets 产出，tracer-bullet 垂直切片 + 阻塞边；前沿 = 01 提取契约 Python 全链、04 resolved_by+收集器，两者可并行）。
- 域：graphify fork（v8-custom @ `b3ea9ed`）× codegraph 优点吸收。基线：merge-plan v4.2（"codegraph.db 事实源"条款将修订）、phase4-plan v1.15（scripts 资产复用）。**phase5 v1.1（graph.json 消除）已作废**，本图取代之，勿再引用其任务拆解。

- 术语见根目录 [CONTEXT.md](../../CONTEXT.md)（事实层 / FTS 缓存 / 提取深度 / 守护进程 / 符号寻址）。

- HITL 票会话应调用 Skill: grilling + domain-modeling。

- 已定结构决策（制图会话裁决，票内细化）：

  1. codegraph 运行时退役，Python 原生重建（Q1）
  2. graph.json/extraction 即事实层，**不建持久 DB**；FTS5 为落盘可重建缓存（Q5/Q7 重构 + Q9）
  3. 图语义保留：无向 `networkx.Graph` + 边属性标方向（`_src`/`_tgt` 形态）+ 自由 relation 词表 + 三档置信度（EXTRACTED/INFERRED/AMBIGUOUS）；多重边现状可接受（Q3）
  4. 守护进程 = **serve 进程内置 watcher**（Q6=A），单进程串行化触发链
  5. 提取深度（signature/docstring/列定位）**在提取层补**，改上游 extractors（Q8=a）
  6. 低自定义维护成本为软约束，非硬性零交叠（Q4）；serve.py 已有 1677 行 fork 补丁（Phase 4，未经上游合并考验），新补丁保持最小

- 优先级：自动同步 / FTS5 / 守护进程 = 高；持久化数据库 = 已消解（不建）。

- 实测基线（2026-09-02，本仓库 corpus）：11606 节点 / 26688 边；AST 符号 7963 个 ID 全唯一（kind+内容 hash）；source\_location 仅行号；**0 节点含 docstring/signature**；列在 tree-sitter `start_point[1]` 有但 [engine.py:1512](../../graphify/extractors/engine.py) 丢弃。

## Decisions so far

- [提取层增强点盘点](tickets/01-extraction-audit.md)：非单一工厂而是"1+26"双轨（engine.py:3004 `add_node` 闭包覆盖 15 个 LanguageConfig 主流语言，26 个专用提取器各自独立）；Python/CL 有 docstring 停留点（回填 \~10 行），JS/TS/Go/Rust 需新写（各 15–30 行）；下游兼容风险仅 3 文件 5 处（文件节点豁免加列、边不加列则 `L110:C5` 全兼容）；总量：主流语言方案约 95–130 行，全语言约 350–450 行。

- [codegraph.db 直查点盘点](tickets/02-codegraph-db-usage-audit.md)：生产代码 17 个 SQL 直查点（7 文件）+ 5 处指纹 + 1 处 sync 子进程 + watch.py 3 处门控；session\_snapshot 与 10 个纯图工具零迁移；**最大难点：edges.metadata 的 resolvedBy/confidence 是 codegraph TS 解析器特产、AST 无源，B3 dispatch 标注与金标整体失效，需 HITL 裁决**；bm25 可确定性等价重建（前提是提取层先补 signature/docstring）；fixture 测试 3 文件 + golden 2 文件 + schema 耦合测试 8 文件。

- [提取字段契约设计](tickets/03-field-contract.md)：六字段契约定档（signature 按 kind 语义化无名字 / docstring 原文+**1500 上限**智能截断+量化超限标记（v1.2 终裁：over-limit 0.86%、99.14% 完整、增量仅 123KB）/ qualified_name `Class::method` 三铁律 / `L110:C5` 起点列 / end_line+end_byte 终点字段），落点为节点顶层字段；首批 = 方案 A（engine 15 语言 + Py/JS/TS docstring，~120–155 行）；实现细则（变量 token 边界截断+标记、grammar 字段名按语言声明、constructor 关联、<5 字符过滤）与参考实现指针（vendored v1.5.0 `getSignature` 钩子模式，无需拉 1.6）全在票内。
- [serve 内置 watcher 架构](tickets/04-serve-watcher-architecture.md)：`graphify/serve_watcher.py`（~250–350 行，纯自定义）+ serve.py 挂载 ≤25 行；增量 = extract per-file cache + 全量 build；watchdog 软依赖（降级 mtime 轮询：5s、scandir 栈、实测与 walk 无差异）；默认关（hook 拉起带开）。**三条实现陷阱**：删除语义（pending 删除集 + extraction 剔除 + MovedEvent 拆 delete/create，防亡灵节点）、graceful shutdown（stop() 阻塞等待批次完成 + 原子写双保险）、codegraph 无轮询降级先例（inotify 耗尽→永久停用，Python 侧参数自定）。
- [FTS5 缓存设计](tickets/05-fts-cache-design.md)：`graphify-out/.fts-index.db` 三表（nodes 元数据全量 / nodes_fts 外部内容表 5 列 bm25 `(0,3,2,0.2,1)` 逐字平移 / graph.json 指纹）；camelCase 分段**做**（TS 符号实测 75.6%）走双向预处理（snippet 只在 docstring 列故无失真）；**语义节点进 FTS**（label 双列同值 = 权重 5 加成，差异化价值面；实测无 description 字段→docstring 列空串起步）；file 节点不进 FTS；外部内容表同步接口提前留口（首期全量 rebuild）；失效两级（watcher 进程内直通 + 重启指纹对比惰性重建）。
- [工具面与脚本迁移语义映射](tickets/06-tool-surface-migration.md)：**dispatch 作为独立概念退役**，语义被边属性 confidence（EXTRACTED 不标/INFERRED/AMBIGUOUS 保留）+ resolver 新增 `resolved_by` 输出（static-type→qualified-name / instance-method 直传 / heuristic→fuzzy）吸收，`_edge_dispatch_info` 双副本与金标删除；gap 通道换源重建（raw_calls 失败收集器 ~15 行作 unresolved_refs 的 AST 等价物，gap_hit 保留）；测试一次性换源（mini fixture → mini graph.json + rebuild_fts 产物；golden 95% 闸门，5% 容差给上游提取覆盖差异）；scripts 重组（run_analysis → analysis-only、rebuild_entry 换编排、adapter 依赖清零、GRAPH_REPORT 直接替换基线不 diff）；15 工具迁移终态表在票内。

## Not yet specified

- 上游 PR 策略终裁（提取增强/字段契约是否回传上游）——**实施完成后**评估（03 已定"本地先行、留路不封死"基调；实施结果决定字段形态是否已稳定）
- 提取增强的语言扩展节奏（Go/Rust docstring、26 个专用提取器/方案 B）——等 FTS 实战验证检索价值

## Out of scope

- 持久化事实源数据库（已裁决不建，Q5/Q7）

- graph.json 消除（phase5 v1.1 方向，作废）

- codegraph 运行时保留路线（Q1 否决）

- SCIP 编译器证据导入（phase4 E 轨道，维持押后）

