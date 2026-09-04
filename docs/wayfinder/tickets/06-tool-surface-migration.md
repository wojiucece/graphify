# 工具面与脚本迁移语义映射

label: wayfinder:grilling
status: closed
assigned: 2026-09-02（本会话）
blocked-by: \[02-codegraph-db-usage-audit.md, 05-fts-cache-design.md]

## Question

Phase 4 的 15 个 MCP 工具与 scripts/ 层（ranked / session\_snapshot / rebuild\_entry / adapter）从 codegraph.db 迁移到新链路的语义映射（HITL）：

- 哪些工具响应可逐字节等价（点查类：get\_node / structure\_queries）

- 哪些语义变化要诚实标注（FTS rank 顺序、freshness 指纹语义、verdict 契约的 degraded 分支）

- db\_fingerprint 的新等价物（graph.json / FTS 缓存的失效指纹，与 05 票联动）

- mini.codegraph.db fixture 测试的改写策略（保留 fixture 换后端 vs 新链路 fixture）

- 15 工具的 `__cg` 消歧后缀语义在纯 graphify id（kind+hash）下的表现

## Resolution

裁决（2026-09-02，Q1–Q4 全采纳 + 用户映射表落档；输入：02 票 17 直查点映射、03 字段契约、04 watcher 触发链、05 FTS 缓存）：

### 裁决表

| 决策点            | 裁决                                                                                                          |
| -------------- | ----------------------------------------------------------------------------------------------------------- |
| Q1 dispatch 通道 | **(b) AST 近似重建，且更进一步：dispatch 作为独立概念退役，语义被边属性** **`confidence`** **+** **`resolved_by`** **吸收**             |
| Q2 gap 通道      | **(b2) extract 失败收集换源**：raw\_calls 解析失败收集器（\~15 行）作 `unresolved_refs` 的 AST 等价物                             |
| Q3 测试改写        | **(a) 一次性 fixture 换源** + golden 验收闸门 **95% 阈值**（5% 容差给 graphify↔codegraph 的上游提取覆盖差异——BM25 等价性结论不覆盖提取差异）     |
| Q4 scripts 重组  | **确认**：run\_analysis → analysis-only；rebuild\_entry 编排壳换源；adapter 依赖清零；GRAPH\_REPORT **直接替换基线（不 diff，不可比）** |

### Q1 细则：dispatch → confidence + resolved\_by（用户映射表）

**confidence 映射**（graphify 边三档直用，用户表 CERTAIN 对齐 EXTRACTED）：

```
EXTRACTED  → 不标注（确定性调用，大多数边）
INFERRED   → 保留（启发式推断）
AMBIGUOUS  → 保留（多态 fanout——dispatch 的核心场景）
```

**resolved\_by 映射**（resolver 新增边属性输出）：

```
static-type     → "qualified-name"   （类型注解驱动 → 限定名匹配）
instance-method → "instance-method"  （实例方法 → 直传）
heuristic       → "fuzzy"            （启发式 → 模糊匹配）
runtime-profiled → 无源，消失
```

**实现前提（实测核实）**：graphify resolution 层现状**无**结构化解析方式输出（resolved\_by 仅存在于 serve.py:1860 的 codegraph 消费代码；resolution.py 的 qualified/heuristic 是内部逻辑不落边属性）——各语言 member-call resolver 需新增 `resolved_by` 边属性输出（resolver 自知解析路径，分类即打点，与 03 提取层增强同批实施）。

**删除清单**：`_edge_dispatch_info` 双副本（adapter.py:217 / serve.py:1846，含 serve 的 `_blast_radius_lines`/`_neighbor_signatures` 消费点）+ `test_dispatch_trace.py:376` 金标 + B3 工具响应的 `dispatch_candidate` 字段。工具 description 诚实标注语义变化（"TS 解析器判定" → "graphify 提取期推断"）。

### Q2 细则：raw\_calls 失败收集器

extract.py:6753 跨文件解析循环内，解析失败的 raw\_call 收集为结构化信号 `{from_node, callee_name, line, file_path}`（现静默丢弃，信息全在循环局部）→ knowledge-gaps.json 换此源重建 + ranked.py `_gap_refs`/gap\_hit 通道保留（token↔失败引用匹配语义不变）。AMBIGUOUS 边建议问题（analyze.py:448）保持独立共存。

### Q3 细则：测试一次性换源

- mini.codegraph.db fixture（test\_adapter / test\_graph\_diff\_sync / test\_run\_analysis）→ 替换为 mini graph.json + `rebuild_fts` 产物，测试逻辑不变只换数据源。

- golden（test\_ranked\_context.py:221/244）：**03 落地后重建金标**，验收阈值 95%（5% 容差 = 上游提取覆盖差异；BM25 评分等价由 02 票结论覆盖）。

- 8 个 schema 耦合测试文件随各工具迁移同步改写，不留双 fixture 中间态。

### Q4 细则：scripts 编排重组

- `run_analysis.py`：删 adapter 依赖（load\_codegraph/_map_\*），输入改为 graph.json 直读 + 分析（god\_nodes/GRAPH\_REPORT/wiki 照旧）——analysis-only。

- `rebuild_entry.py`：保留 mkdir 锁 / stale 接管壳，内部换成"上游 extract → build → to\_json → rebuild\_fts"编排（04 触发链的手动入口形态）；db\_fingerprint 字段换 graph.json (mtime\_ns, size)（状态文件 schema v2）。

- adapter 依赖清零后：import 链清理；pyproject 若有 codegraph 相关依赖评估移除（vendored codegraph/ 副本处置进收尾票）。

- **GRAPH\_REPORT 重基准**：首次新链路 rebuild 后立即跑完整报告 → 覆盖旧基准 → 提交为新基线；不做 diff 对比（kind 词表/边集/gaps 源全变，不可比）。efficiency benchmark 同模式改打新链路接口。

- hooks（sessionend/precompact/sessionstart）不动（rebuild\_entry 内部迁移）。

### 落档说明（无需裁决项）

- **E** **`__cg`** **消歧后缀**：纯 graphify id（kind+hash）原生形态无碰撞场景，get\_node 的 `__cg` 回退点查逻辑删除。

- **F** **`_slice_source`** **切片原语升级**：行范围切 → `end_byte` 字节切（03 已补字段，get\_node body 档精确切片）。

### 工具面迁移终态（15 MCP 工具一览）

| 工具                                                                                                                                                         | 迁移                                                                  | 终态                                         |
| ---------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------- | ------------------------------------------ |
| 10 个纯图工具（query\_graph/get\_community/god\_nodes/graph\_stats/shortest\_path/find\_dead\_code/get\_untested\_symbols/list\_prs/get\_pr\_impact/triage\_prs） | 零迁移                                                                 | 不变                                         |
| get\_node（B2）                                                                                                                                              | 元数据点查 → .fts-index.db nodes 表（03 字段补齐）；`__cg` 回退删；切片 end\_byte 化    | 结构不变，id 原生形态                               |
| get\_neighbors / blast\_radius（B3）                                                                                                                         | `_edge_dispatch_info` 删；dispatch 标注由边属性 confidence/resolved\_by 承载  | 响应少 dispatch\_candidate 字段（description 标注） |
| get\_ranked\_context（B1）                                                                                                                                   | ranked.py 换 .fts-index.db（05 schema，bm25 逐字平移）；gap\_hit 换源保留        | 排序等价，金标 95% 闸门                             |
| get\_changed\_symbols（C3）/ get\_hotspots（C4）                                                                                                               | git\_symbols 查询换 graph.json 内存索引（source\_file 建索引 O(n)）或 FTS 缓存元数据表 | 语义不变（度数单位差异诚实标注，02 票）                      |
| freshness（\_derive\_freshness）                                                                                                                             | WAL mtime vs graph.json → **FTS 缓存 mtime vs graph.json**（05 指纹平移）   | verdict 语义不变                               |

