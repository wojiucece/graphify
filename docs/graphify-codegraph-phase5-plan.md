# graphify × codegraph Phase 5 — graph.json 消除方案

> ## ⚠️ 已作废（2026-09-04 收尾）
>
> 本方案（v1.1，graph.json 消除 / 单一事实源 materialize 路线）**已作废**——决策链
> 由 `docs/wayfinder/MAP.md` 取代（MAP 明确"phase5 v1.1（graph.json 消除）已作废，
> 本图取代之，勿再引用其任务拆解"）。native-indexing 定稿：**graph.json 与 extract
> 产物保留为唯一事实层**（与 phase5 的"graph.json 彻底退役"方向相反），不建持久事实源
> 数据库，`.fts-index.db` 仅作可重建缓存。本文档保留为历史决策记录，不再作为实施依据。

**版本**：v1.1（2026-09-02 边界修订——v1.0 的"只干融合路径、独立模式保留 graph.json"与单一事实源主张自相矛盾（把导出产物误标为事实源），经裁决改为**全面收敛**：①统一为单一架构模型"事实源 → materialize 内存物化 → 派生视图"，"独立模式"叙述消亡，只剩同一管线的两种事实源（codegraph.db 或 extraction.json）；②独立路径不搞增量锚点改造，增量语义收敛为 extract cache + on-demand rebuild，图层永远全量确定性重建；③graph.json **彻底退役**（v1.0 曾议的可选导出方案否决），不再写入任何产物，全部消费方处置见 Task 8–11。新增 Task 8–11、R24。已作废，见上横幅。）
**日期**：2026-09-02
**基线**：

- Phase 4 深化方案 v1.14（`docs/graphify-codegraph-phase4-plan.md`）

- phase4-deepening worktree（SDD 37 commits，终审 Ready to merge，含 serve 15 工具 / ranked.py / session\_snapshot.py / structure\_queries.py / git\_symbols.py）

**定位**：Phase 4 完成后的架构收敛动作。把"两层坐标系持久化"（事实层 + graph.json 合并派生层）收敛为**单一事实源模型**：有 codegraph 的项目事实源是 `.codegraph/codegraph.db` + `semantic-seed.json`；无 codegraph 的项目事实源是 extract 原生产物 `extraction.json`（新增落盘，Task 8）。合并图改为 serve 启动时内存物化的纯派生产物，两路径共用同一条 materialize 链。目标不是省一个文件，而是消除双写一致性负担、shrink-guard 复杂度与 node\_link 序列化往返，并终结"graph.json 既是运行时读依赖又是导出物"的角色混淆。

***

## 1. 可行性与边界

### 1.1 graph.json 内容分类（vs 两种事实源）

graph.json 无任何"仅 graph.json 持有、且无法从别处重现"的数据：融合路径由 `codegraph.db + semantic-seed.json` 经确定性函数算出；独立路径由 extract 原生产物算出（v1.0 时期该产物不落盘、graph.json 兼任事实源，v1.1 起落盘为 `extraction.json`，见 §1.2）。

| graph.json 内容                                                    | 来源                                          | 消除后如何重现              |
| ---------------------------------------------------------------- | ------------------------------------------- | -------------------- |
| 节点 `id/label/source_file/source_location/kind/language`          | codegraph.db `nodes` 表（adapter 映射）          | materialize 内存重算，确定性 |
| 边 `source/target/relation/confidence/source_file/synthesized_by` | codegraph.db `edges` 表 + relation-rank 去重   | 同左                   |
| `__cg` 消歧后缀 / 折叠 / dangling 修剪                                   | adapter + build 确定性逻辑                       | 同左                   |
| `norm_label` / `confidence_score`                                | `label`/`confidence` 派生                     | 同左，O(1)              |
| **`community`** **/** **`community_name`**                       | `cluster()`（Leiden/Louvain）                 | **内存重算，唯一有成本项**      |
| 语义节点（`kind=None` 等）                                              | graphify `extract()` → `semantic-seed.json` | **种子文件保留，不在 db**     |
| `hyperedges`                                                     | `semantic-seed.json`                        | 种子文件保留               |
| `knowledge_gaps`                                                 | 已独立 sidecar（`knowledge-gaps.json`）          | 不受影响                 |
| `_learning_overlay`                                              | 已独立 sidecar（reflect）                        | 不受影响                 |

实测（graphify\_fork 自身 corpus）：nodes 11146、links 25724，**全部节点带 community**；语义节点（kind=None）486 个——证明社区与语义面确实存在于 graph.json，但均有可重建源。

### 1.2 边界：全面收敛，"独立模式"叙述消亡（v1.1 重写）

v1.0 的边界（"融合路径收敛、独立模式不动"）与单一事实源主张自相矛盾：既然架构主张是"事实源 → 内存物化 → 派生视图"，graph.json 在任何路径下都不该再充当事实源——它此前在独立路径的角色是**导出产物被误标为产品本体**。v1.1 起统一为**单一架构模型、两种事实源**：

| 项目形态                        | 事实源                                                | 物化链                                                              |
| --------------------------- | -------------------------------------------------- | ---------------------------------------------------------------- |
| 有 `.codegraph/codegraph.db` | codegraph.db + semantic-seed.json                  | materialize（Task 1，adapter 映射）                                   |
| 无 `.codegraph/`             | **extraction.json**（extract 原生产物，v1.1 新增落盘，Task 8） | materialize\_from\_extraction（与左共用同一条"无向 build → 往返 → DiGraph"链） |

关键裁决（用户 2026-09-02 定夺）：

1. **独立路径不做增量锚点改造**：不把上游 update 的"读旧图做部分更新"逻辑迁移到新事实源，增量语义收敛为 **extract cache（per-file 增量提取，上游已有）+ on-demand rebuild（图层全量确定性重建）**。materialize 全量重建万级图实测 \~1-2s（R20 量级），watch/serve 场景可接受；上游 incremental 的图级合并、shrink-guard、merge-driver 在两条路径上一并退役。
2. **graph.json 彻底退役**：不降级为可选导出（v1.0 备选项否决）。全部产物不再包含 graph.json；全部读消费方（CLI 查询面 / serve / watch / merge-graphs / global\_graph / affected）改物化或处置（Task 8–11）；对外承诺由 serve + CLI 物化承接。
3. **存量迁移**：已有 graph.json、无 extraction.json 的项目做一次性 rebuild（extract cache 在，成本≈增量提取）；迁移期 CLI 的 `--graph` 参数保留只读兼容（可直接加载旧 graph.json 供对比验证），新产物一律走新事实源。

触发面已有天然分叉（`precompact/sessionend` hook 的 `[ -d .codegraph ]` 分支）继续沿用，但分叉的语义从"两种模式"降级为"两种事实源的选择"，物化链与下游视图完全同一。

### 1.3 核心正确性约束：重建必须复刻"无向 build → 往返 → DiGraph"

现在 serve 的 G 是这条链的产物：

```
build_from_json(directed=False)   # 无向 nx.Graph，A→B/B→A 反向重复边折叠为一条（#1061 first-seen 方向胜出）
→ to_json                         # node_link_data 落盘，还原 _src/_tgt 为 source/target，写 directed:False
→ _load_graph                     # node_link_graph 强转 directed:True → DiGraph
```

若直接改用 `build_from_json(directed=True)` 建图，`graphify/build.py` 的反向重复边折叠分支（`if not G.is_directed() and G.has_edge(src, tgt) ... continue`）被跳过（G 已是有向），A→B 与 B→A 会各成一条边——**图变了，15 个工具的遍历结果漂移**。因此 materialize 必须复刻"无向 build + `_src`/`_tgt` 还原方向"这条往返，产出与今天逐字节同构的 DiGraph。

***

## 2. 总体设计：materialize 物化函数

新建 `graphify/codegraph_context.py`（graphify 包内，供 serve / run\_analysis / session\_snapshot / CLI 查询面 import），单一重建路径、两个事实源入口（v1.1）：

```
materialize(db_path, seed_path=None, root=None) -> (G_digraph, communities)      # 融合路径事实源
materialize_from_extraction(extraction_path, root=None) -> (G_digraph, communities)  # 独立路径事实源
    两者共用内层链：
    1. 取 extraction dict（前者 load_codegraph(db)+合并 seed；后者直接读 extraction.json）
    2. G_u = build_from_json(extraction, root=root)   # 无向，保持折叠语义
    3. communities = cluster(G_u)                      # 无向图上算，与现状一致
    4. G_digraph = _to_directed(G_u)                   # 内存版 node_link 往返：_src/_tgt 还原方向、剥临时键
    5. 附派生键：community / norm_label / confidence_score（等价 to_json 的附键行为）
    6. 附 G.graph：hyperedges / _logical_directed=False / _learning_overlay（out 目录 sidecar）
```

`db_fingerprint(db_path) -> (MAX(files.indexed_at), WAL mtime_ns)` 一并上移（现于 `scripts/rebuild_entry.py`，serve 热重载需要）。

### 2.1 adapter 映射上移

`scripts/adapter.py` 的 `load_codegraph`（含 `_map_nodes`/`_map_edges`/`_disambiguate_ids`/`_map_knowledge_gaps`）上移到 `graphify/codegraph.py`，使包内 serve 可干净 import。`scripts/adapter.py` 保留为薄 re-export（`from graphify.codegraph import *`），`run_analysis.py`/`rebuild_entry.py`/`ranked.py` 及既有测试的 `from adapter import ...` 不破。

**理由**：codegraph 适配器是融合项目的一等输入源，归宿在 graphify 包内自洽；scripts/ 的自定义面（run\_analysis/ranked/session\_snapshot）本就 `sys.path` 挂 graphify 包，反向上移无循环。§3.2 映射口径一字不动。

***

## 3. 任务拆解

### Task 1 materialize 模块 + adapter 上移（其余任务的前置）

**Files**：

- Create：`graphify/codegraph_context.py`（materialize + db\_fingerprint）

- Create：`graphify/codegraph.py`（adapter 映射上移，`load_codegraph`/`validate_semantic_anchors`）

- Modify：`scripts/adapter.py`（薄 re-export，保留既有 import 面）

- Create：`tests/test_codegraph_context.py`

**设计要点**：

- `_to_directed(G_u)` 等价复刻 `_load_graph` 的 node\_link 往返：遍历无向边，按 `_src`/`_tgt` 还原 `source/target` 建 DiGraph，边属性剥掉 `_src`/`_tgt`；节点属性原样带入。

- 派生键与 `to_json` 对齐：`norm_label = _strip_diacritics(label).lower()`；`confidence_score` 由 `confidence` 映射 `_CONFIDENCE_SCORE_DEFAULTS`；`community_name` 仅在 `community_labels` 非空时写（现状 run\_analysis 传空 dict → 不写，保持空）。

- `materialize` 返回的 G 与"`to_json(G_u)` 落盘后 `_load_graph` 加载"的 G 逐节点/逐边/逐属性同构——作为单测断言（同 db+seed fixture）。

**验收**：

- [ ] `materialize` 输出与旧"读 graph.json"路径的 G 图同构（含方向、边属性、节点属性、`_logical_directed`）。

- [ ] `db_fingerprint` 与 `rebuild_entry.db_fingerprint` 输出一致（相同 db）。

- [ ] `scripts/adapter.py` 薄壳下 `from adapter import load_codegraph` 仍可用（既有 test\_adapter 全绿）。

### Task 2 serve.py codegraph 模式

**Files**：

- Modify：`graphify/serve.py`（`_GraphContextCache` + `_load_ctx` + `_resolve_graph_path`）

**设计要点**：

- **模式检测**（v1.1 三级）：`project_path/.codegraph/codegraph.db` 存在 → 融合路径，走 `materialize`；否则 `graphify-out/extraction.json` 存在 → 独立路径，走 `materialize_from_extraction`；两者皆无 → 既有 graph.json 兼容读（迁移期，Task 8 一次性 rebuild 后消亡）。

- **热重载 key**：融合路径用 `db_fingerprint(db) + seed mtime_ns`，独立路径用 `extraction.json (st_mtime_ns, st_size)`，替代原 `(st_mtime_ns, st_size)`（对 graph.json）；`_GraphContextCache` 的 LRU + pinned 语义不变，仅换 load 源与 key 构造。

- `_resolve_graph_path` 在 codegraph 模式解析为 db+seed 路径对；缺省 graph 同理取 project 根。

- 保持 `_get_trigram_index` 预热与 `_communities_from_graph` 的语义：materialize 直接返回 communities，不再从节点反推（但保留反推函数作迁移期兼容读分支用）。

**验收**：

- [ ] codegraph 项目：删掉 graph.json 后 serve 正常启动，15 工具查询全绿。

- [ ] db 变更（如 codegraph sync 后）下一次查询命中新图（热重载仍工作）。

- [ ] 独立路径项目（无 `.codegraph`、有 extraction.json）：serve 正常启动、15 工具查询全绿；extraction.json 变更后热重载命中新图（v1.1 新增）。

- [ ] 迁移期兼容：只有旧 graph.json 的项目 serve 可读旧图（只读），与改造前行为逐字节一致（test\_serve 全绿；Task 8 rebuild 后该分支消亡）。

### Task 3 run\_analysis 停写 graph.json

**Files**：

- Modify：`scripts/run_analysis.py`

**设计要点**：

- 删除 `to_json` + shrink-guard 块（现 L173–193）。`knowledge-gaps.json` / `GRAPH_REPORT.md` / `semantic-seed.json` 照写。

- 统一复用 materialize：`G, communities = materialize(...)`，`god_nodes/surprising_connections/find_import_cycles` 三种无向语义分析在 `G.to_undirected()` 视图上跑（DiGraph 每条有向边对应无向一条边，语义等价），`generate_report` 照旧。

- `wiki=True` 的 `to_obsidian(G, communities, ...)` 照旧（吃内存图，不落 graph.json）。

- shrink-guard #479 在 codegraph 模式下失去持久锚点，随本任务停用（v1.1：独立路径 `update` 的 #479 随 Task 8 一并退役，两路径对称）。

**验收**：

- [ ] codegraph 项目 rebuild 后 `graphify-out/` 下不生成 graph.json。

- [ ] 同 db+seed 下 `GRAPH_REPORT.md` 与改动前逐字节一致（语义等价硬证明）。

- [ ] `--wiki` 仍产出 wiki/。

### Task 4 session\_snapshot 改调 materialize

**Files**：

- Modify：`scripts/session_snapshot.py`

- Modify：`tests/test_session_snapshot.py`

**设计要点**：

- `_load_graph_json` → `materialize(db, seed, root)` / `materialize_from_extraction(extraction, root)`（v1.1：独立路径项目也出快照），取 `G.degree()` top-N 与 community 标题；不再读 graph.json。

- db/seed 路径从 project root 推导（`<root>/.codegraph/codegraph.db`、`<root>/graphify-out/semantic-seed.json`；无 db 时 `<root>/graphify-out/extraction.json`），沿用现有 argv\[1]=root 入口。

- 撕裂读竞态防护（R5-4）改作用于事实源指纹（db 指纹或 extraction mtime）：指纹读取失败 → 空快照 + 日志，压缩流程不受影响。

**验收**：

- [ ] 同 db+seed 下快照文本与旧"读 graph.json"路径逐字节一致。

- [ ] 独立路径（无 db、有 extraction.json）：快照文本与旧"读 graph.json"路径逐字节一致（v1.1 新增）。

- [ ] 无 db 且无 extraction（未建图项目）→ 空快照，`sessionstart` 注入脚本 sys.exit(0)。

### Task 5 hooks 门条件

**Files**：

- Modify：`scripts/sessionstart-graphify-server.sh`（L17）

**设计要点**：`[ ! -f "graphify-out/graph.json" ]` → `[ ! -d ".codegraph" ] && [ ! -f "graphify-out/extraction.json" ]`（任一事实源在即启动 server；v1.1：独立路径项目同样有 server，不再是 codegraph 项目专属）。`precompact/sessionend` hook 已按 `.codegraph` 分叉，语义降级为"事实源选择"（快照内容两路径同构，Task 4），逻辑无需改。

**验收**：

- [ ] codegraph 项目（无 graph.json）sessionstart 正常拉起 server。

- [ ] 独立路径项目（仅 extraction.json）sessionstart 正常拉起 server（v1.1 新增）。

- [ ] 无任何事实源的目录：不启动 server，行为不变。

### Task 6 diff() 废弃

**Files**：

- Modify：`scripts/run_analysis.py`（`diff()` 函数）

**设计要点**：`diff(prev_graph, db_path)` 依赖前置图快照，无 graph.json 后失去 prev 锚点。职责由 C3 `get_changed_symbols`（git 轴）承接——"这次图里变了什么"走 git diff，不走图内对比（与 v1.14 §5-C3 的 graph\_diff 诚实空标记裁定同源）。codegraph 模式移除 `diff()`；非 codegraph 路径不受影响（`diff` 不在 serve 工具面）。

**验收**：

- [ ] 无调用方报 `diff` 缺失；C3 changed\_symbols 测试保持绿。

### Task 7 测试收尾

**Files**：

- Modify：`tests/test_run_analysis.py`（断言 graph.json 不再生成）

- Modify：`tests/test_serve.py` / `tests/test_serve_http.py`（codegraph 模式用例 + 独立路径 extraction 用例 + 迁移期兼容读回归）

- Modify：`tests/test_session_snapshot.py`

- Create：`tests/test_codegraph_context.py`（已在 Task 1）

**验收**：

- [ ] worktree 全量 pytest 绿（旧 graph.json 断言类测试改挂迁移期兼容读或等价性断言，不裸删）。

- [ ] check-custom.sh 登记新测试文件名。

### Task 8 extract 落盘 extraction.json + update 收敛（v1.1 新增，独立路径事实源）

**Files**：

- Modify：`graphify/cli.py`（extract/build 流水线落盘原生产物；update 收敛）

- Create：`tests/test_extraction_json.py`

**设计要点**：

- extract 流水线在 `build_from_json` 之前把原生产物 dict（`{nodes, edges, hyperedges, knowledge_gaps, ...}`，即 `build_from_json` 的入参形态）落盘为 `graphify-out/extraction.json`（原子写，沿用上游 atomic-writes 纪律）。

- `to_json` 写 graph.json 的调用点（cli.py extract/build 主链路）删除；`GRAPH_REPORT.md` / `graph.html` / wiki 照旧（吃内存 G）。

- **update 收敛为 rebuild**：`update` = extract（per-file 增量 cache，上游已有，不触碰）+ materialize 全量。上游"读旧 graph.json 做图级部分合并"的 incremental 分支与 #479 shrink-guard 在独立路径退役（与 Task 3 融合路径的停用对称）。`--force` 语义不变（清 cache 全量提取）。

- 存量迁移：有旧 graph.json、无 extraction.json 的项目，首次 `update`/`query` 自动触发 rebuild（extract cache 命中则成本≈0）。

**验收**：

- [ ] extract 后 `graphify-out/extraction.json` 存在；同输入下 materialize\_from\_extraction 与旧"读 graph.json"路径 G 图同构（复用 Task 1 断言形态）。

- [ ] 产物目录不再出现 graph.json；`GRAPH_REPORT.md`/`graph.html` 逐字节一致。

- [ ] update（含增量 cache 命中与 --force 两分支）后查询结果与全量 rebuild 一致。

### Task 9 CLI 查询面读锚替换（v1.1 新增）

**Files**：

- Modify：`graphify/cli.py`（`query` / `explain` / `path` / `diagnose` / `cluster-only` / `tree` / `benchmark` 等命令的图加载点）

- Modify：`graphify/affected.py`（`--graph` 输入）

**设计要点**：

- 各命令的"加载 graph.json"调用点统一替换为事实源感知加载（db→materialize；extraction.json→materialize\_from\_extraction；旧 graph.json→兼容只读，迁移期对比验证用）。替换点集中在少数加载函数处，保持补丁最小 diff（R24 对策）。

- `--graph` 参数保留但语义降级为"直接加载指定 graph.json"（只读调试/对比通道，不再是默认产物路径）。

- `merge-graphs` 合并层从 graph 层下移到 extraction 层：合并 extraction dict 的 nodes/edges（原始层合并比 DiGraph 序列化合并更干净，id 归并语义不变），输出 merged extraction.json。

**验收**：

- [ ] 同事实源下 `query`/`explain`/`path` 输出与旧 graph.json 路径逐字节一致。

- [ ] `merge-graphs` 产物经 materialize 后节点/边并集与旧图级合并等价。

### Task 10 watch 写回 + global\_graph + merge-driver 处置（v1.1 新增）

**Files**：

- Modify：`graphify/watch.py`（增量写回链路）

- Modify：`graphify/global_graph.py`

- Modify：`graphify/cli.py`（merge-driver 注册面）

**设计要点**：

- watch.py 的 node\_link 落盘（L1156 附近）改为落 extraction 增量产物（复用 Task 8 落盘形态：更新 extraction.json）；serve 感知走 Task 2 的 extraction 指纹热重载。上游 watcher 的防抖/降级（fork 已移植）不动。

- `global_graph`（`~/.graphify/global-graph.json`）落盘形态同步降层为 extraction 合并产物；读取侧走物化。

- merge-driver（git union-merge graph.json）：无默认产物可合并，命令与注册面退役（保留函数级 deprecation 提示一个版本周期）。

**验收**：

- [ ] watch 触发后 extraction.json 更新、serve 热重载命中新图；graph.json 不再生成。

- [ ] `global add` 两项目后 global 查询可用；merge-driver 命令显式退役提示。

### Task 11 对外承诺与文档面更新（v1.1 新增）

**Files**：

- Modify：`README.md`（三件套叙述）、`docs/how-it-works.md`、CLI help 文本（`__main__.py`）

**设计要点**：产物叙述从三件套（graph.html / GRAPH\_REPORT.md / graph.json）改为两件套 + 查询承诺改由 `graphify query`/serve 物化承接；`--graph` help 改为只读兼容口径。纯文档/help 面，不触碰逻辑。

**验收**：

- [ ] README/帮助文本无 graph.json 作为产物的叙述；命令 help 与实际行为一致。

***

## 4. 总验收标准

- [ ] codegraph 项目 `graphify-out/` 下**不生成 graph.json**；serve 15 工具、快照、report、wiki 全绿。

- [ ] 独立路径项目同样**不生成 graph.json**，事实源 extraction.json 就位；`query`/`explain`/`path`/update/watch 全链路在 extraction.json 上行为等价（输出与旧 graph.json 路径逐字节一致）（v1.1 新增）。

- [ ] 同 db + 同 seed 下：`materialize` 产出的 G 与旧"读 graph.json"路径的 G 图同构（含方向/边属性/节点属性）。

- [ ] db 变更后下一次查询命中新图（热重载仍工作）。

- [ ] 迁移期兼容：仅存旧 graph.json 的项目可只读加载（`--graph` 通道与 serve 兼容分支），行为与改造前逐字节一致；新产物一律走新事实源。

- [ ] efficiency benchmark 比例不回退（merged/grep 的 13.9% 基准）。

***

## 5. 风险登记册（增补 R20–R23）

| #   | 风险                                                                                                                                            | 等级         | 对策                                                                                                                                                              |
| --- | --------------------------------------------------------------------------------------------------------------------------------------------- | ---------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| R20 | `cluster()` 内存重算使 serve 冷启动/热重载变慢（万级图 native Leiden \~1.4s，超大图更慢）                                                                             | 中          | 首次查询才物化（惰性）；实测万级图门 < 数秒；超大图（10 万+节点）再评估降级策略                                                                                                                     |
| R21 | 重建语义漂移（`directed=True` 直建 vs 往返）                                                                                                              | 高（已识别，可规避） | materialize 复刻"无向 build + 往返"，§1.3 为硬约束；图同构单测锁死                                                                                                                 |
| R22 | 热重载 key 从 mtime 换 db 指纹，漏检 DB 变化                                                                                                              | 低          | 复用 `db_fingerprint`（MAX(indexed\_at)+WAL mtime），误报方向=多重建一轮，安全（F7 语义）                                                                                            |
| R23 | adapter 上移破坏 scripts 层既有 import                                                                                                               | 低          | 薄壳 re-export + 全量 pytest；`test_adapter.py` 不破                                                                                                                   |
| R24 | 独立路径收敛触碰上游核心（cli.py 查询/流水线面、watch.py 写回、export.py/global\_graph.py），fork 与 upstream/v8 合并冲突面从 scripts/ + 少量补丁扩大到包核心数处，周更级上游迭代下维护成本上升（v1.1 新增） | 中高         | ①读/写锚点各自集中到少数加载/落盘函数再整体替换，补丁保持最小 diff；②上游图级 incremental/merge-driver 相关改动此后无需吸收（语义已退役），冲突双向减少；③月度 merge 人工核 cli.py/watch.py；④check-custom.sh 登记全部改动文件防 merge 丢改 |

***

## 6. 非目标（明确不做）

1. ~~不动 graphify 独立模式~~（v1.1 废除：独立路径并入收敛，"独立模式"叙述消亡，见 §1.2）。
2. **不引入派生类新持久文件**：唯一新增持久物是 `extraction.json`——定性为 extract 原生产物（事实源），非派生 sidecar（v1.1 修订）；community 仍不落盘，纯内存重算（用户已定夺）。
3. **不改 codegraph schema / 双向写回**：沿用 v4.2 否决决策。
4. **不改 adapter §3.2 映射口径**：上移只换 import 路径，不换折叠/去重/消歧逻辑。
5. **不迁移上游图级 incremental 逻辑**（v1.1 新增）：独立路径增量 = extract cache + on-demand rebuild，不把"读旧图部分合并"逻辑搬到新事实源（用户已定夺）。

***

## 7. 决策登记

**已定夺（2026-09-02 用户裁决，v1.1 落档）**：

1. **边界全面收敛**："独立模式"叙述消亡，统一为单一架构模型 + 两种事实源（codegraph.db / extraction.json）；独立路径不迁移图级 incremental，增量为 extract cache + on-demand rebuild。
2. **graph.json 彻底退役**：不降级为可选导出；全部产物与读消费方处置见 Task 8–11；`--graph` 仅保留迁移期只读兼容。

**方案代定（审阅时可否决）**：

1. **adapter 上移**：映射逻辑进 `graphify/codegraph.py`，`scripts/adapter.py` 留薄壳。
2. **diff() 废弃**：codegraph 模式移除，由 C3 changed\_symbols 承接"变了什么"。
3. **实施落点**：Phase 4 已合并（`b3ea9ed`），本 Phase 建议开 `feat/phase5-graph-json-removal` 新分支/worktree 实施；文档已落 `docs/`（v1.0 §7-4 所记"编辑受限"已解除）。

