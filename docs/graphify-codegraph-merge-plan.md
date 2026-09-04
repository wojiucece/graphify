# graphify × codegraph 合并方案报告

> ## ⚠️ 已被 native-indexing 取代（2026-09-04 收尾）
>
> 本方案（v4.2，分层融合路线：codegraph 作符号事实层 + 只读适配器）**已由
> `docs/graphify-native-indexing-spec.md`（原生索引能力，codegraph 运行时退役）整体取代**。
> 实施结论：codegraph TS 运行时退役，其四大优点（自动同步 / FTS5 全文检索 / 守护进程 /
> 随时查询）在 graphify Python 自定义层原生重建；**graph.json + extract 产物是唯一事实层**，
> 不建持久事实源数据库（"codegraph.db 事实源"条款作废），`.fts-index.db` 仅作可重建缓存，
> watcher 内置于 serve 进程。决策链与退役 ADR 见 `docs/adr/0001-retire-codegraph-runtime.md`。
> 本计划文档保留为历史决策记录（当时语境下的正确选择，被后续架构裁决取代），不再作为实施依据。
> 文中所有"codegraph 作主索引 / 适配器 / .codegraph/codegraph.db 事实源"表述均指已退役方案。

**版本**：v4.2（grill 质询决策落档：适配器位置、semantic 锚定规范、PR 时机、meta 图索引、验收口径五项已定。已取代，见上横幅）
**日期**：2026-08-28
**数据基线**：

- Graphify-Labs/graphify @ `v8` 分支（0.9.51，Apache-2.0）[cite:1][cite:2]
- colbymchenry/codegraph @ `main`（v1.6.0，2026-08-26 发布，MIT）[cite:5][cite:10]
- 实施基座：`D:\code\graphify_fork`（`v8-custom` @ `0.9.51+fork.1`，2026-08-28 晨已同步 upstream/v8）；内含 vendored codegraph v1.5.0 参考副本（gitignore，仅参考）

所有标注"实测"的数据来自 2026-08-28 对本地 fork 与 7 个存量项目 graph.json 的全量审计及两侧源码核读（脚本见附录 10.3）[cite:11]。

---

## 1. 执行摘要

合并方向为**分层融合**：codegraph 作符号事实层（主索引），graphify 作分析理解层（派生视图），~200 行只读适配器桥接，融合发生在数据层而非代码层（§2）。本地 fork 已验证双轨路径：数据层走适配器，算法层走剪枝移植（watch 防抖/降级已落地）。

存量资产处置：7 个项目的 graph.json 按 A 类（3 个纯代码，归档重建）/ B 类（4 个含 semantic，种子整体携带迁移，其中 2 个停滞建议冻结）处置，语义知识 100% 保留（§4）。

**上游吸收（§5）**：两侧均高度可行——graphify 侧走 fork 合并流（0.9.20→0.9.51 已 11+ 次合并零重大冲突，合并新增物几乎全为新文件，唯一热点是 analyze.py 的两个小补丁）；codegraph 侧不 fork（二进制消费 + schema 版本门控，实测 v8→v9 仅加列）。例行维护成本合计约每月 1–2 小时。

**hook 与 MCP（§6）**：两侧 MCP 工具面零命名冲突（codegraph 全带前缀，graphify 无前缀），职能互补（符号精确查询 vs 知识层分析），可同时注册；hook 侧推荐拓扑为 codegraph watcher 独占索引职能、graphify 侧改造为 sync 后的分析重建触发器；核心决策是 id 空间统一（适配器直传 codegraph id，需折叠碰撞检测）。

实施节奏：Phase 0+1（2.5 天）验证核心假设 → +1.5（累计 3 天）完成存量资产接管 → +2（累计 4.5–5.5 天）工程可用。存量组合最大图实测 18,108 节点，全图路线即可（§7）。

---

## 2. 合并方向：分层融合，而非单向吸收

### 2.1 方向判定

**结论：不是"graphify 融合 codegraph"，也不是反向吸收，而是分层融合**——"代码库不合并，数据经适配器单向流动，算法按需剪枝移植"。

| 候选方向 | 形态 | 判定 |
|---|---|---|
| A. graphify 吸收 codegraph | 提取内核并入 graphify（Python 重写） | ✗ 否决 |
| B. codegraph 吸收 graphify | 分析管线并入 codegraph（TS 重写） | ✗ 否决 |
| **C. 分层融合（本方案）** | codegraph 主索引 + graphify 分析层 + 只读适配器 | ✓ 采用 |

否决 A：codegraph 提取内核（框架感知动态分发边合成、ReferenceResolver、原生 FS 自动同步）在 Python 中重写成本高且永久双线维护；语言生态错位（TS vs Python 分析栈）。否决 B：Leiden/多模态/wiki 生成的 Python 科学计算生态无 TS 等价物；纯图算法虽可移但收益不抵成本。

选 C 的依据：① 资产互补而非重叠（提取 vs 理解）；② 耦合最小（适配器只依赖 graphify 4 个稳定入口——契约由上游 `test_architecture_doc.py` 强制 [cite:3]——加 codegraph schema 版本门控）；③ 方向可对冲（两上游正在互相进入对方领地，若一方全覆盖，适配器退化为薄壳，退出成本最低）；④ 双宽松许可下衍生边界干净（§9）。

### 2.2 本地 fork 基座的既成事实（实测）

1. **算法层移植已落地**：fork 提交 `ce98376` 已将 codegraph watcher 的自适应防抖/降级/重试上限移植进 `watch.py`（含 MIT 署名）——运行时算法级吸收被验证为约一个补丁的体量。
2. **上游跟随模型已验证**：0.9.20→0.9.51 共 11+ 次 upstream/v8 合并，自定义面收敛于 `watch.py`/`serve.py`/`install.py`/hook 脚本，冲突低。**适配器与迁移脚本作为 `v8-custom` 自定义提交交付**，复用该流程。
3. **vendored codegraph 副本为 v1.5.0**（落后一个次版本）；实测其与上游 main 的 schema 差异仅 `files.generated` 一列（迁移 v9），核心表零变更，可作开发参照。v1.6.0 的 WAL 修复（静置上限 64MB，`CODEGRAPH_WAL_HEAL_MB`）与只读连接策略相关（§3.3）。

---

## 3. 总体架构与数据设计

### 3.1 总体架构

```
                       ┌─────────────────────────────────────┐
                       │  codegraph（TypeScript，持续运行）    │
                       │  tree-sitter → .codegraph/codegraph.db │
                       │  (WAL, FTS5, 自动同步, MCP server)    │
                       └──────────────┬──────────────────────┘
                                      │ 只读打开 (mode=ro)
                                      ▼
┌────────────────────────────────────────────────────────────────┐
│  适配器 adapter.py（新增，~200 行，Python sqlite3）               │
│  nodes/edges → graphify 提取 schema · 版本门控 · 多重边折叠        │
└──────────────┬─────────────────────────────────────────────────┘
               ▼
┌──────────────────────────────────────────────┐   ┌─────────────────────────┐
│ graphify 分析管线（v8，库独立使用）             │   │ graphify 概念层（可选）  │
│ build_from_json → cluster(Leiden/Louvain)    │   │ LLM/Vision 语义遍历      │
│ → god_nodes / surprising_connections          │   │ docs/PDF/图像/视频       │
│ → find_import_cycles / graph_diff             │   │ 写入同一图（跨模态边）    │
│ → report.generate                             │   └─────────────────────────┘
└──────────────┬───────────────────────────────┘
               ▼
   graphify-out/：GRAPH_REPORT.md · graph.json · wiki/ · callflow
```

设计原则：**只读适配，不写回 `.codegraph/`**（与 codegraph 迁移体系和自动同步零冲突）；**派生视图**（分析是索引的纯函数，索引变则重算）；**主从分工**（codegraph 管代码精确查询，graphify 管宏观分析与人面消费）。graphify v8 代码解析已确定性化（tree-sitter、无 LLM [cite:2]），LLM 仅处理非代码模态——两层数据在"确定性"维度同质，融合语义鸿沟小。

### 3.2 数据映射（经两侧源码确认）

| codegraph（`.codegraph/codegraph.db` [cite:8]） | graphify 提取 schema | 处理 |
|---|---|---|
| `nodes.id` | `id` | 直传（§6.3 id 空间统一） |
| `nodes.qualified_name`（空则 `name`） | `label` | 直传 |
| `nodes.file_path` | `source_file` | 直传 |
| `nodes.start_line` | `source_location` | 拼为 `L{n}` |
| `nodes.kind` / `nodes.language` | 节点属性 | 透传（kind 替代 label 启发式判定文件节点） |
| `edges.kind` | `relation` | 词表直映（`calls/imports/contains/references/extends/...`） |
| `edges.provenance` | `confidence` | `'heuristic'` → `INFERRED`；NULL → `EXTRACTED` |
| `edges.metadata`（JSON） | 边属性 | 展开（保留 `synthesizedBy`） |
| `unresolved_refs`（status='failed'） | Knowledge Gaps 输入 | 适配器附加导出 |

**源码核读补充（v4.1，grill 核查）**：① build 对层级的判定是 `_origin` 优先、无标记时按形状回退（`source_location` 匹配 `^L\d` 即 AST 层，`_is_ast_tier`）——适配器输出 `L{n}` 即自动归入 AST 层，**无需自标 `_origin`**；② 同节点对的边折叠部分内建（generic 关系 `references/uses/mentions` 在折叠中必让位于具体关系，`_GENERIC_RELATIONS`）——适配器的折叠优先级只需覆盖 generic 之外的组合（如 `calls` vs `imports` vs `contains`）；③ codegraph 文件节点实测存在（`kind:'file'`，tree-sitter.ts L509）。

### 3.3 并发与一致性

- **只读打开**：`sqlite3.connect("file:...?mode=ro", uri=True)`；**禁用 `immutable=1`**（WAL 下漏读未 checkpoint 数据）。WAL 模式读不阻塞写（codegraph `busy_timeout=5000` 刻意置于 pragma 之首 [cite:7]），只读并发安全。
- **版本门控**：启动时 `SELECT MAX(version) FROM schema_versions`，高于已测试版本即报错退出（已测试值以首次 `codegraph init` 后的实测 DB 为准——迁移 v9 属上游 main，是否随 v1.6.0 发布未验证，见 §5.2）；codegraph 迁移体系为增量式（实测 v8→v9 仅加列 [cite:6]）。
- **长连接策略**：分析为短任务（开→读→关）；若驻留（如 MCP 挂载），周期重开连接规避 WAL checkpoint 语义。
- 目录名可被 `CODEGRAPH_DIR` 覆盖（Windows/WSL 分离索引，issue #636）[cite:8]。

### 3.4 依赖

| 组件 | 推荐 | 说明 |
|---|---|---|
| Python | 3.10–3.12 | `[leiden]` extra 仅支持 < 3.13（现有 fork 环境为 3.10，直接可用）；3.13+ 自动回退 networkx Louvain（零重依赖，质量略降） |
| graphify | `uv tool install graphifyy[leiden]` | v8 明确支持库独立使用 [cite:3] |
| codegraph | 官方安装（自包含二进制或 npm） | 适配器零依赖其运行时，只读 DB 文件 |
| 聚类 | graspologic_native → graspologic → Louvain | v8 三级回退内建 [cite:9] |

---

## 4. 存量 graph.json 迁移方案

### 4.1 存量清单与分类（实测，2026-08-28）

| 项目 | 最后构建 | 节点（ast/semantic） | semantic 边结构 | 分类 | 处置 |
|---|---|---|---|---|---|
| `D:\code`（根，跨仓 meta 图） | 08-20 | 18,108 / 0 | 无 | A | 归档重建（见 4.2 特例） |
| `D:\code\graphify_fork` | 08-13 | 13,451 / 485 | 71 条：56 内部 + 12 ast→ast + 3 跨层 | B | 种子迁移（首选模板） |
| `D:\code\wuziqi` | 08-25 | 354 / 28 | 18 条：100% 内部 | B | 最简种子迁移 |
| `D:\BusinessAnalysis`（旧格式） | 06-29 | 707 / 183 | 241 条触及 semantic | B+停滞 | 冻结，激活时迁移 |
| `D:\BusinessAnalysis\bilibili` | 07-15 | 296 / 0 | 无 | A | 归档重建 |
| `D:\quant_project`（旧格式） | 07-15 | 2,760 / 169 | 115 条触及 semantic | B+停滞 | 冻结，激活时迁移 |
| `D:\quant-research` | 07-15 | 1,233 / 1 | 无 | A | 归档重建 |

两项结构性实测事实（决定迁移设计）：

1. **semantic 层准自包含**：全组合 semantic 边锚点 100% 为文件级节点（.md/.csproj/.razor），**0 条锚到函数/类等代码符号**——种子整体携带即可存活，无需 id 重映射。
2. **两种格式代际并存**：旧格式（BusinessAnalysis 根、quant_project，2026-06/07 构建）无 `_origin` 标记，用 `file_type ∈ {concept, document, rationale, image}` 启发式判别（已验证有效）；新格式直接按 `_origin` 切分。

### 4.2 执行顺序

- **A 类**：`codegraph init` + 适配器重建，旧图移入日期备份目录。
- **B 类**：fork（模板）→ wuziqi → 停滞 B 类（BusinessAnalysis 根、quant_project）按激活时点排队；semantic 资产（219 个 concept、84 个 rationale，LLM 产出）随种子保值。
- **D:\code meta 图特例（已决策 Q4：保留单索引）**：跨仓纯代码图（扫描 graphify_fork/edge/doubao1/wuziqi 等）为组合中最活跃的使用面（08-20 构建）。在 `D:\code` 根 `codegraph init` 保 workspace 视图 + 各活跃仓库另建 per-repo 索引用于日常编码（`.codegraph` 自忽略，嵌套索引互不干扰；代价仅重复索引的磁盘，SQLite 几百 MB 内）。

### 4.3 技术路径：种子整体携带

```
旧 graph.json ──拆分脚本──▶ semantic_seed.json
                              ├─ semantic 节点（id 原样携带）
                              ├─ semantic 边（端点原样携带）
                              ├─ 锚点文件节点（被引用的 ast 文件级节点，id 原样携带）
                              └─ hyperedges（semantic 成员超边）

codegraph.db ──适配器──▶ 代码 extraction dict ──┐
                                              ├──▶ build_from_json ──▶ 新图
semantic_seed.json ──────────────────────────┘
```

要点：

1. 种子节点 id 原样携带，与适配器输出 id 空间零翻译——semantic 子图凭闭合性存活。
2. 唯一冲突点：锚点文件与适配器重复产出同文件节点（实测全组合仅 4 个 .py 锚点引用）——按 (source_file, label) 就地改指，分钟级工作量。
3. shrink-guard 会拦截首次迁移（ast 换血致节点数骤减）：先归档旧图再生成。
4. `build_from_json` 原生接受双路合并输入（消费普通 `{nodes, edges}` 字典），无契约障碍。

### 4.4 验收标准

- B 类：semantic 节点数迁移前后 100% 相等；semantic 边存活率 ≥ 99%（fork 预期 71/71、wuziqi 18/18）；失联边显式列入 Knowledge Gaps。
- A 类：新图对旧图 ast 文件节点覆盖率 ≥ 95%（按 source_file 口径，差异须可解释为提取差异）。
- 新图通过 dangling endpoints 校验；迁移后 `report.generate` 无空段落、社区数与迁移前同量级。

---

## 5. 上游更新吸收可行性（新增）

**结论：两侧均高度可行，且方向 C 的结构本身即为可持续吸收而设计**——graphify 侧走 fork 合并流（已验证），codegraph 侧根本不 fork（二进制消费 + 版本门控）。例行维护合计约每月 1–2 小时。

### 5.1 graphify 上游（v8 分支，0.9.x 周更级）

| 项 | 评估 |
|---|---|
| 吸收方式 | fork `v8-custom` 定期 `git merge upstream/v8` |
| 已验证记录 | 0.9.20→0.9.51 共 11+ 次合并，自定义面（watch/serve/install/hooks）冲突低 |
| 合并新增物的冲突面 | `adapter.py`、`split_semantic_seed.py`、`run_analysis.py` 均为**新增文件，零冲突**（落地于 fork 顶层 `scripts/` 目录，与上游包结构 `graphify/` 零交叠——已决策 Q1；id 折叠碰撞检测与多重边折叠实现在 adapter.py 内，不碰上游文件）；唯一热点是 `analyze.py` 的两个小补丁（`_is_file_node` 改判 kind、betweenness 采样护栏——后者可移至调用侧 `run_analysis.py`，则热点收敛为一个） |
| 契约保障 | 只依赖 4 个入口（`build_from_json`/`cluster`/`analyze`/`report`），上游以 `test_architecture_doc.py` 强制该契约不漂移 [cite:3] |
| 例行成本 | 每月 1 次、0.5–2 小时（merge + 重放补丁 + 用 fork 自身图跑回归 fixture） |

降低热点的三个工程手段：① betweenness 护栏可在调用侧（`run_analysis.py`）包装采样，不改上游；② `_is_file_node` 的 tsx 缺失属通用 bug，**Phase 2 本地验证补丁有效后再向上游提 PR**（已决策 Q3：先证明方案再上游化，被拒绝则补丁永久保留 fork，成本可接受）——被接受后热点文件归零；③ 补丁保持最小 diff，避免夹带重构。

### 5.2 codegraph 上游（v1.6.0，两日内仍在发版）

| 项 | 评估 |
|---|---|
| 吸收方式 | 不 fork——npm/二进制升级重装；vendored 副本仅参考，同步方式为重新 clone |
| 兼容性风险面 | schema 演进（唯一实质风险）；实测 v1.5.0→main 仅 `files.generated` 加列（迁移 v9），增量演进无破坏先例 [cite:6] |
| 防线 | ① `schema_versions` 门控（超实测版本即 fail loudly；门控基准值以首次 `codegraph init` 后的实测 DB 版本录入——迁移 v9 在上游 main，是否随 v1.6.0 发布未验证）；② 升级后跑适配器冒烟（fork 图为 fixture）；③ 月度 diff 上游 `schema.sql`/`migrations.ts` |
| 例行成本 | 按需/季度级 0.5 小时；当前锁定 v1.6.0 |
| 破坏情形代价 | 若未来版本改列/改表：更新 §3.2 映射表（集中单点，约半天）；MCP 工具面变化与本合并无关（agent 直连，不经过适配器） |

### 5.3 极端情形与吸收节奏

- **一方停更/转向闭源**：Apache-2.0/MIT 允许永久持有当前版本继续演进；分层结构下冻结一侧不牵连另一侧。
- **两上游功能趋同**（graphify 确定性化已在向 codegraph 领地延伸 [cite:2]，codegraph 预告 hosted platform）：适配器退化为薄壳或移除，退出成本全方案最低（§2.1 对冲依据）。
- **节奏建议**：graphify 月度合并（跟随 0.9.x）；codegraph 季度或按需升级（changelog 只需关注 schema 与工具面条目）。

---

## 6. 运行期集成：hook 与 MCP（新增）

### 6.1 MCP 工具面共存（两侧源码实测）

| graphify serve.py（10 工具 + 2 Resource） | codegraph tools.ts（8 工具） | 职能 |
|---|---|---|
| `query_graph` / `get_node` / `get_neighbors` / `get_community` / `god_nodes` / `graph_stats` / `shortest_path` | `codegraph_search` / `codegraph_node` / `codegraph_explore` | 检索/导航 |
| — | `codegraph_callers` / `codegraph_callees` / `codegraph_impact` | 符号精确查询 |
| `list_prs` / `get_pr_impact` / `triage_prs` | — | PR 层分析 |
| Resource: `graphify://report` / `graphify://stats` | `codegraph_status` / `codegraph_files` | 状态 |

- **零命名冲突**：codegraph 工具全带 `codegraph_` 前缀，graphify 无前缀——同一 agent 客户端可同时注册两个 MCP server，无遮蔽。
- **职能互补不重叠**：agent 的符号精确问题（"改这里影响谁"）走 codegraph；知识层问题（"这个库的枢纽/社区/概念关联"）走 graphify。合并后 graphify 侧 `query_graph`/`get_node` 返回的图中即含 codegraph 供给的代码节点。
- **进程形态**：codegraph MCP 带常驻 daemon（query-pool、watchdog、会话管理，engine.ts 延迟初始化设计）；graphify serve.py 为轻量进程。并存内存开销可接受。
- **消费分工建议**：agent 用 codegraph MCP 做编码期查询；graphify 的知识层查询给 agent 挂 MCP，同时保留 wiki 文件输出供无 MCP 的 agent 依文件导航（双出口）。

### 6.2 hook 拓扑（合并后的推荐形态）

现状事实：graphify 的安装面是 per-platform 安装器——Claude 系写 CLAUDE.md 段落 + PreToolUse hook（`graphify hook-guard search/read`，nudge agent 优先查图而非裸 grep/read），**trae-cn 平台原生支持**（bundle 复用）；codegraph 侧是多 agent 安装器（Claude Code/Cursor/Codex/opencode 等，47 个幂等安装契约测试）。**codegraph 安装器是否覆盖 Trae 未验证**——若不覆盖，codegraph MCP 需在 TRAE 手动注册（标准 MCP 配置，工具面不受影响）。

合并后的推荐拓扑：

1. **codegraph 的 FS watcher 独占代码索引职能**（默认自动同步，保持不动）——它是唯一索引源。注意其监听范围**仅代码文件**（按扩展名过滤，sync/watcher.ts 源码核读），.md/PDF/图像变更不触发 codegraph sync。
2. **graphify 侧 watch.py 仅退役"代码索引"职能，保留非代码文件监听**——semantic 层（docs/图像的 LLM 提取）的触发只能由 graphify 侧承担；代码侧则改造为"codegraph sync 完成后 debounce 触发分析重建"（复用已移植的快窗/退避逻辑），分析重建为分钟级（18k 节点量级），按需触发（显式命令或 sync 后静默期），不逐文件。
3. **避免双 watcher 重复索引**：若 graphify watch 仍独立索引代码文件，与 codegraph 重复劳动且写 graphify-out——合并完成判据之一即关闭该职能（仅代码部分，非代码监听保留）。
4. **PreToolUse hook 保留并增益**：合并后 hook-guard nudge agent 查询的图中含 codegraph 供给的代码节点，nudge 的收益直接扩大；codegraph 自身的 agent hook 与 graphify hook 可共存（不同注册点、幂等安装）。

### 6.3 id 空间统一（合并后 MCP 的核心影响）

- **决策：适配器直传 codegraph 节点 id**（§3.2），使 `get_node` 与 `codegraph_node` 操作同一 id 空间，agent 跨 server 引用符号无需翻译。
- **技术前提**：graphify build 的 `_normalize_id` 会把非 `\w` 字符折叠为下划线（`Foo::bar` → `foo_bar`）——codegraph 符号 id 为 sha256 十六进制串、文件 id 为 `file:${路径}`（实测 `generateNodeId`，tree-sitter-helpers.ts），后者折叠后为 `file_` 前缀，碰撞概率低但仍需适配器做**折叠碰撞检测**（10 万级节点分钟内可查），碰撞时追加消歧后缀。
- **id 稳定性边界（源码核读，v4.1）**：符号 id = `sha256(路径:kind:名称:行号)`——确定性，但**符号移动行号即变 id**；文件 id = `file:${路径}`，完全稳定。
- **运行规范（已决策 Q2，固化）**：**semantic 引用一律锚定文件级节点，符号级引用视为易失**——存量数据恰好如此（锚点 100% 文件级）是起点而非依据；规范落地为 semantic 子代理输出校验的一条规则（Phase 3 实施），确保后续 LLM 提取不再产生符号级易失引用。
- **对旧 semantic 种子无影响**：种子端点为文件级节点，id 原样携带、自身闭合；后续 semantic 子代理引用"当前图 id"（即 codegraph id 风格），链条自洽。
- **PR 层工具的前提**：`list_prs`/`triage_prs` 依赖 git 数据，与 codegraph 无交集，不受合并影响。

---

## 7. 实施阶段与验收标准

### 7.0 阶段难度-收益总览

| 阶段 | 工期 | 难度 | 主要技术不确定性 | 核心收益 | 收益 |
|---|---|---|---|---|---|
| Phase 0 规模实测 | 0.5 天 | ★☆☆☆☆ | 无 | 定分级路线；产出节点/边计数与 kind 分布 | ★★★☆☆ |
| Phase 1 只读适配器 PoC | 2 天 | ★★★☆☆ | schema 漂移（门控对冲）；多重边折叠优先级；id 折叠碰撞 | 全链路打通，任何 codegraph 索引仓库产出报告/wiki/graph.json | ★★★★★ |
| Phase 1.5 旧图迁移 | 0.5 天 | ★★☆☆☆ | 已实测消解（锚点 100% 文件级）；仅 4 个 .py 锚点需改指 | 存量资产无损接管，semantic 知识 100% 保留 | ★★★★☆ |
| Phase 2 护栏与补丁 | 1–2 天 | ★★★★☆ | betweenness 采样质量取舍；大仓聚类内存 | 万级文件仓库稳定可用；报告质量修复 | ★★★★☆ |
| Phase 3 集成消费 | 1–2 天 | ★★★☆☆ | sync 钩子时机；双 watcher 拓扑切换 | `graph_diff` 知识层变更追踪；无 MCP agent 靠 wiki 导航；watch 职能切换（§6.2） | ★★★★☆ |
| Phase 4 跨模态深化（可选） | 1–2 周起 | ★★★★★ | LLM 成本与输出确定性 | 双层图（代码↔docs↔图像）、跨模态惊喜连接 | ★★★☆☆ |

**推荐节奏**：最小可行 = Phase 0+1（2.5 天）；资产接管 = +1.5（累计 3 天）；工程可用 = +2（累计 4.5–5.5 天）；Phase 3/4 按需。阶段间严格串行。**全程可逆**：A/B 类旧图只归档不删除，任一阶段均可回退 graphify 原管线重跑。

### Phase 0 — 规模实测（0.5 天）

```powershell
cd <目标仓库> && codegraph init
python -c "import sqlite3; c=sqlite3.connect(r'.codegraph/codegraph.db'); print('nodes', c.execute('SELECT COUNT(*) FROM nodes').fetchone()[0]); print('edges', c.execute('SELECT COUNT(*) FROM edges').fetchone()[0])"
```

通过门：≤ 30 万节点全图；30–100 万按 kind 筛选后分析；> 100 万采样或放弃全图 betweenness 类 O(V·E) 算法。**存量组合实测最大 18,108 节点，低于最低档 16 倍以上——当前组合全图路线即可**；门控保留用于将来新仓库。首选试验场：`D:\code\graphify_fork` 自身（13,936 节点含 485 semantic，同时验证 Phase 0 与迁移路径）。

### Phase 1 — 只读适配器 PoC（2 天）

- 产出：`adapter.py`（§3.2 映射 + 版本门控 + 多重边折叠 + id 折叠碰撞检测）+ `run_analysis.py`，落地 fork 顶层 `scripts/`（Q1）；作为 fork `v8-custom` 自定义提交。
- 验收（Q5 决策：不引入 NMI/ARI 量化——标注基线成本与收益不成比例）：Gin（~110 文件）与 Django（~3k 文件）跑通全链路；社区划分与目录结构人工比对合理（Django 的 ORM/Views/URLs 落不同社区）；god nodes 与公认核心吻合（Gin 的 `Context`、Django 的 `QuerySet`，白名单制 go/no-go）。

### Phase 1.5 — 旧图迁移（0.5 天）

- 产出：`split_semantic_seed.py`（种子拆分 + 4 个 .py 锚点改指 + 旧格式 file_type 判别，约百行）；执行序见 §4.2。存量审计已完成（§4.1，脚本见附录 10.3）。
- 验收：§4.4 全条目。

### Phase 2 — 护栏与补丁（1–2 天）

- betweenness 采样护栏（调用侧包装）；`_is_file_node` 改判 kind（同步向上游提 PR，见 §5.1）；多重边折叠优先级调参。
- 验收：VS Code 规模（~10k 文件）端到端分钟级完成；报告无空段落、无崩溃。

### Phase 3 — 集成消费（1–2 天）

- `graph_diff` 接 codegraph sync 事件自动差分；启用 `wiki/` 与 `callflow` 导出；**执行 §6.2 的 watcher 拓扑切换**（graphify watch 退役代码索引职能、改造为重建触发器）；按 §6.1 注册双 MCP、按 §6.3 验证 id 直传；**落地 §6.3 的 semantic 锚定校验规则**（符号级引用拒收或降级为文件级，Q2）。
- 验收：一次 `codegraph sync` 后 `graph_diff` 输出与 `git diff --stat` 文件级变更对得上；双 watcher 确认只剩一个索引源；跨 server 用同一 id 查到同一节点。

### Phase 4 — 可选深化（不在本期承诺范围）

- 跨模态层：LLM 语义遍历覆盖 docs/ADR/PDF，与符号图合并为双层图（跨模态加权已内建）。
- 双向写回：明确**不推荐短期做**（侵入 codegraph schema 与迁移体系，收益不明确）。

---

## 8. 风险登记册

| # | 风险 | 等级 | 对策 |
|---|---|---|---|
| R1 | 仓库符号规模超预期，全图算法不可行 | 低（实测校准） | 存量最大 18,108 节点，全图路线即可；门控保留用于新仓库；Louvain 回退省内存 |
| R2 | codegraph schema 演进破坏适配器 | 中 | 实测增量演进（v8→v9 仅加列）；`schema_versions` 门控上限以实测 DB 版本为准；月度 diff 上游 schema 文件 |
| R3 | WAL/只读连接细节 | 低 | `mode=ro` 禁 `immutable=1`；短任务连接；驻留周期重开（v1.6.0 的 WAL 修复不改变该策略） |
| R4 | 语言集合与词表不一致（codegraph 22+ vs graphify ~36–40） | 中 | 映射表集中单点；未知 kind 透传不丢弃；以 codegraph 为主索引 |
| R5 | graphify 迭代极快（0.9.x 周更级） | 中 | fork 已验证 11+ 次合并流程；只依赖 4 个契约入口；analyze.py 补丁保持最小 diff（§5.1） |
| R6 | 上游方向变化（graphify 商业平台 / codegraph hosted platform） | 中 | 只依赖各自开源核心；双宽松许可允许独立衍生；分层结构退出成本最低 |
| R7 | 存量 semantic 边迁移失联 | 低（实测校准） | 锚点 100% 文件级、0 符号级；种子整体携带下预期失联仅 4 个 .py 锚点；失联边显式入 Knowledge Gaps |
| R8 | vendored 参考副本过期误导开发 | 低 | 实测 schema 差异仅一列，核心表零变更；以 `codegraph init` 实际 DB 为最终契约 |
| R9 | 旧格式 graph.json 判别错误漏迁 semantic 资产 | 低 | file_type 启发式已验证有效；迁移脚本分代际处理并输出判别统计供复核 |
| R10 | id 折叠碰撞（codegraph id 经 `_normalize_id` 后同名） | 低 | 适配器做折叠碰撞检测，碰撞追加消歧后缀（§6.3） |
| R11 | 双 watcher 重复索引（合并过渡期） | 低 | Phase 3 拓扑切换明确退役 graphify 代码索引职能；幂等安装器保证重装安全 |

---

## 9. 许可与合规结论

- **graphify：Apache-2.0** [cite:1][cite:4]；**codegraph：MIT** [cite:5]。均允许商用、修改、分发与私用衍生，无 copyleft 传染。
- 义务：保留各自版权与许可证文本；Apache-2.0 侧如有 NOTICE 需一并携带。
- fork 现状（实测）：已含 Apache-2.0 LICENSE、NOTICE（MIT→Apache 再许可声明存档）；watch.py 移植以行内注释署名（合规）；后续整文件移植建议在 NOTICE 集中追加条款。适配器读 DB 不涉及 codegraph 代码复制，无新增义务。

---

## 10. 附录

### 10.1 关键命令

```powershell
# 符号规模（Phase 0）
python -c "import sqlite3;c=sqlite3.connect('.codegraph/codegraph.db');print(c.execute('SELECT kind,COUNT(*) FROM nodes GROUP BY kind ORDER BY 2 DESC').fetchall())"

# schema 版本（适配器门控）
python -c "import sqlite3;c=sqlite3.connect('.codegraph/codegraph.db');print(c.execute('SELECT * FROM schema_versions').fetchall())"

# 合成边规模（INFERRED 边占比）
python -c "import sqlite3;c=sqlite3.connect('.codegraph/codegraph.db');print(c.execute('SELECT provenance,COUNT(*) FROM edges GROUP BY provenance').fetchall())"

# 存量项目 graph.json 审计（A/B 分类）
python -c "import json,collections;d=json.load(open('graphify-out/graph.json',encoding='utf-8'));n=collections.Counter(x.get('_origin') for x in d['nodes']);print('nodes',dict(n));print('class:','A' if not n.get('semantic') else 'B')"
```

### 10.2 来源

- [cite:1] GitHub API — Graphify-Labs/graphify: https://api.github.com/repos/Graphify-Labs/graphify
- [cite:2] graphify v8 README（确定性解析、平台表、extras）: https://github.com/Graphify-Labs/graphify/blob/v8/README.md
- [cite:3] graphify v8 ARCHITECTURE.md（管线契约与强制测试）: https://github.com/Graphify-Labs/graphify/blob/v8/ARCHITECTURE.md
- [cite:4] graphify.com: https://www.graphify.com
- [cite:5] GitHub API — colbymchenry/codegraph: https://api.github.com/repos/colbymchenry/codegraph
- [cite:6] codegraph src/db/schema.sql: https://github.com/colbymchenry/codegraph/blob/main/src/db/schema.sql
- [cite:7] codegraph src/db/index.ts（连接 pragma）: https://github.com/colbymchenry/codegraph/blob/main/src/db/index.ts
- [cite:8] codegraph src/directory.ts（DB 路径、CODEGRAPH_DIR）: https://github.com/colbymchenry/codegraph/blob/main/src/directory.ts
- [cite:9] graphify v8 graphify/cluster.py（三级回退）: https://github.com/Graphify-Labs/graphify/blob/v8/graphify/cluster.py
- [cite:10] GitHub Commits API — codegraph main（v1.6.0 发布链）: https://api.github.com/repos/colbymchenry/codegraph/commits?per_page=5
- [cite:11] 本地实测与源码核读（2026-08-28）：fork git 历史比对、7 项目 graph.json 全量审计、vendored v1.5.0 与上游 schema 规范化 diff、MCP 工具面/id 生成/watcher 范围/tier 判定源码核读（serve.py、codegraph src/mcp/tools.ts、extraction/tree-sitter-helpers.ts、sync/watcher.ts、graphify/build.py）

**时点声明**：GitHub 数据为 2026-08-28 查询值；基准数字均为项目自报，未独立复现。codegraph 安装器对 Trae 的支持情况未验证（§6.2）。

### 10.3 实测脚本存档

- [存量图全量审计脚本](computer://d:\Users\hd487\Documents\tare solo\work-mode-projects\6a90e13d5196b8362f911377\audit_legacy_graphs.py)
- [旧格式图深审脚本](computer://d:\Users\hd487\Documents\tare solo\work-mode-projects\6a90e13d5196b8362f911377\audit_legacy_format.py)
- 运行环境：Windows PowerShell + Python 3.10；只读操作。
