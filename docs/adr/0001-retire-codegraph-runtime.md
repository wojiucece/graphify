# ADR-0001：退役 codegraph TS 运行时，Python 原生重建索引能力

- 状态：Accepted（2026-09-04，native-indexing 收尾落档）
- 决策链：[wayfinder 地图](../wayfinder/MAP.md)（7 票决议 01–07，精确实现坐标在各决策票 Resolution：`docs/wayfinder/tickets/01–07`）→ [实施 spec](../graphify-native-indexing-spec.md)（label: ready-for-agent）→ 实施票 `docs/issues/01–11`（验收清单；精确实现坐标见 wayfinder 决策票 tickets/01–07 的 Resolution）
- 关联文档：[合并方案 v4.2（已取代）](../graphify-codegraph-merge-plan.md)、[Phase 4 深化 v1.15（已取代）](../graphify-codegraph-phase4-plan.md)、[Phase 5 v1.1（作废）](../graphify-codegraph-phase5-plan.md)

## 为什么是 ADR

按 ADR 三条件（难逆转 / 无上下文会惊讶 / 真实权衡）全部命中：

1. **难逆转**：codegraph 运行时退役后，vendored v1.5.0 参考副本删除、`.codegraph/` 目录与 gitignore 条目移除、双运行时维护链条拆除。重新引入 codegraph 需要重建 node 依赖链、schema 门控、watcher/daemon 进程管理，成本高于当初退役收益。
2. **无上下文会惊讶**：codegraph-merge（分层融合：codegraph 主索引 + graphify 分析层 + 只读适配器）曾在 2026-08-28 定稿并投入实施（Phase 0–4 交付，b3ea9ed）。未经本 ADR 的上下文，读者无法理解为何同一周内方向翻转——不是"融合失败"，而是四问题评估后架构裁决替换。
3. **真实权衡**：codegraph 提供四大优点（自动同步 / FTS5 全文检索 / 后台守护进程 / 随时查询），是真实能力；退役它并非零成本，而是评估了"Python 自定义层重建"与"继续消化外部运行时"的净收益后决策。

## Context

graphify fork（`D:/code/graphify_fork`，v8-custom）在 2026-08 经 codegraph-merge 方案引入 codegraph（TypeScript 运行时）作为符号事实层：持久化数据库 `.codegraph/codegraph.db`、FTS5 全文检索、文件监听自动同步（watcher/daemon）、后台守护进程。`serve.py`/`ranked.py`/`scripts/` 经只读适配器（adapter.py）单向消费 codegraph DB。

实施与审计发现四个问题（spec §Problem Statement）：

1. **双运行时负担**：Python 之外维护一条 node 进程链（watcher/daemon/sync），Windows 下进程管理与排障成本高。
2. **坐标系割裂**：符号数据在 codegraph.db，分析与语义在 graph.json，两者经适配器单向流动——每条链路（serve 查询、ranked 检索、快照、重建编排）都要跨坐标系 JOIN。
3. **检索面残缺**：graphify 自身产物不含 signature/docstring（实测 11606 节点 0 覆盖），检索深度受制于 codegraph 的 schema 与生命周期。
4. **能力不可持续**：codegraph 演进（WAL、schema 迁移、TS 解析器元数据）全部是外部不可控变量，fork 每月都要消化。

## Decision

退役 codegraph TS 运行时，其四大优点在 graphify 的 Python 自定义层原生重建（wayfinder 7 票决议 Q1–Q9 综合）：

- **单一事实层**：graph.json 与 extract 产物是唯一事实层；**不建持久事实源数据库**（".codegraph/codegraph.db 事实源"条款作废）。
- **FTS5 检索**：由落盘可重建缓存 `.fts-index.db`（sqlite FTS5，三表 nodes/nodes_fts/meta）承载，删除无损、指纹失效自动重建（spec §FTS 缓存，票 05）。
- **自动同步**：由 serve 进程内置 watcher（`graphify/serve_watcher.py`）承载，单进程内串行触发链（spec §Watcher，票 04/10）。
- **提取深度**：signature/docstring/qualified_name/列定位/end_line/end_byte 六件套在提取层原生补（票 01–03），`resolved_by` 与 `failed_refs` 失败收集器承载 dispatch 语义与知识缺口（票 04）。
- **工具面**：点查/ranked/结构性工具换源 `.fts-index.db` + graph.json（票 06/07/08）；dispatch 作为独立概念退役，语义由边属性 confidence + resolved_by 吸收。
- **编排**：单一重建入口 `scripts/rebuild_entry.py`（extract→build→to_json→rebuild_fts→分析，票 09）；三触发面（watch 代码事件 / SessionEnd / PreCompact hook）统一改指。
- **vendored 副本处置**：vendored codegraph v1.5.0 参考副本（仅参考、gitignore）删除；MIT 署名保留在吸收处代码注释（`graphify/watch.py` 移植 codegraph 防抖/退避算法处等）。

graphify 的图语义原样保留：无向 networkx.Graph、边属性标方向、自由 relation 词表、强制三档置信度（EXTRACTED/INFERRED/AMBIGUOUS）。

## Consequences

### 正面

- 单一事实层消除跨坐标系 JOIN；`graph.json` 即事实源，任何派生（FTS 缓存、报告、wiki、knowledge-gaps）可由它确定性重建。
- 零 node 运行时依赖；Windows 进程链（watcher/daemon/sync）退役，守护进程 = serve 内置 watcher。
- 检索面补齐 signature/docstring（实测新链路 AST 符号 100% 携带六件套，FTS 深度远胜旧链路 0 覆盖）。
- 上游合并成本回到"新能力集中新文件、上游补丁最小化"，月度合并仍小时级。
- 测试一次性换源（mini graph.json + rebuild_fts 产物），不留双 fixture 中间态。

### 负面 / 代价

- **双轨退役期一次性成本**：存量项目（6+ 个）逐个首次新链路重建，产物 id 方案变化（path-id 取代 function:md5）；金标默认根（`D:/code/graphify_fork`）在重建前保持 SKIP（conftest 金标门，见 §金标门点亮）。
- **排序等价性**：bm25 schema/权重/tokenizer 逐字平移保证排序等价，但索引内容（label 带 `()`、rationale 排除、id 方案变化）与旧链路非逐字节一致——金标闸门 95% 容差吸收（spec §Testing Decisions）。
- **watcher 软依赖**：watchdog 为 pyproject extras 软依赖（`watch=[watchdog]`），缺失自动降级 mtime 轮询（spec §Watcher）。保留该 extras 声明（serve_watcher 使用）。
- **`graphify watch` CLI 增量路径**：退役 codegraph 判别后，watch CLI 增量走上游内部 pipeline（graph.json 更新携带 failed_refs，FTS 由 serve 指纹惰性重建）；全量重建由 hooks/serve_watcher 统一走 rebuild_entry。
- **split_semantic_seed 锚点改指退役**：原 `--codegraph-db` 按 source_file 把旧图锚点改指为 codegraph 文件节点 id——codegraph 退役后无 DB 可对且新链路文件 id 是 path 式，改指目标已不存在；存量旧图 seed 迁移由迁移方在目标图做 id 映射。

### 未决 / 后续

- 上游 PR 回传终裁（提取增强/字段契约是否回传上游）——实施完成后评估，基调本地先行、留路不封死。
- 提取增强的语言扩展（Go/Rust docstring、26 个专用提取器）——等 FTS 实战验证检索价值。
- SCIP 编译器证据导入维持押后。

## 金标门点亮（I4）

conftest 金标门（`tests/conftest.py` `_golden_gate`）默认根 `GRAPHIFY_GOLDEN_ROOT=D:/code/graphify_fork`；金标数据（`tests/fixtures/ranked_golden.json`）已按新链路 path-id 重建（票 07），仅当指向的 graph.json 顶层含 `failed_refs`（新链路事实层形态）时点亮（实测通过率 100%，20/20）。

- **主 checkout 基线重建后默认根自动点亮**（协调项：`D:/code/graphify_fork` 首次新链路重建后，默认根不再 SKIP）。
- **CI/隔离环境**：显式设 `GRAPHIFY_GOLDEN_ROOT` 指向已新链路重建的语料；`-m 'not golden'` 可整组排除。

## 验证摘要（2026-09-04，worktree 实证）

- 新链路全链路重建（rebuild_entry 首次 rebuild，AST-only 零 token 成本）：graph.json 16642 节点，含六件套契约（signature 7274 / qualified_name 7458 / docstring 4203 / `L:C` 列定位 7458）+ 顶层 `failed_refs` 3888 条；`.fts-index.db` 投影生成。GRAPH_REPORT 基线：16653 节点 · 30451 边 · 1087 communities（97% EXTRACTED / 3% INFERRED）。
- 四层 seam 测试通过 + 金标 95% 闸门 100%（`GRAPHIFY_GOLDEN_ROOT=<worktree>`，2 passed；默认根 `D:/code/graphify_fork` 旧数据仍 SKIP）。
- efficiency benchmark 换源 `.fts-index.db` 后重跑，结果 JSON 落档 `benchmarks/results-2026-09-04.json`（直接替换基线不 diff）：merged 20950 tokens / grep_read 259982 = **8.1%**（旧基线 13.9%），merged hit@5 0.917 / grep_read 0.583（金标 pass 率 100%）。
- 全量测试通过（5278 passed / 262 skipped / 39 failed——39 项全部为环境性 pre-existing，与 base 818a22d 失败集逐项 IDENTICAL，本票零新失败；清单含 test_install/hooks/uninstall/settings_merge/terraform/non_regular_files/provider/ollama/claude_md PreToolUse 断言（fork 有意禁用，CLAUDE.md）/watch 两处 Windows cwd 锁等。以 `PYTHONPATH=<worktree>` 跑，环境依赖 `python -m graphify` 子进程）。
