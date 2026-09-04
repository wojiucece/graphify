# FTS5 缓存设计

label: wayfinder:grilling
status: closed
assigned: 2026-09-02（本会话）
blocked-by: [03-field-contract.md]

## Question

FTS 缓存（落盘可重建、定性缓存非事实源，Q9 已定）的设计（HITL）：

- 文件位置与命名（`graphify-out/.fts-index.db` 或同类）
- schema：FTS5 虚表列 + 元数据表
- 索引字段面：label / norm_label / signature / docstring / kind / source_file（等 03 票字段契约定型）
- 构建时机：serve 启动惰性构建 vs 首次查询触发；万级符号构建耗时实测
- 失效指纹：graph.json 的 (mtime, size) 还是内容 hash；与 02 票盘点的 db_fingerprint 语义对齐
- 查询入口：CLI 子命令 / MCP 新工具 / ranked.py BM25 通道切换（02 票盘点为输入）
- tokenizer：unicode61 对代码符号（snake_case、驼峰、下划线）的适配；codegraph 用的 tokenizer 可参考 vendored 副本

## Resolution

裁决（2026-09-02，Q4/Q5/Q6 全采纳推荐 + 用户细则；输入：03 票字段契约、02 票 bm25 等价性结论、codegraph `.codegraph/codegraph.db` 实测 schema）：

### 裁决表

| 决策点 | 裁决 |
| --- | --- |
| FTS 列集 | **(a) 严格 5 列对齐** codegraph：`id/name/qualified_name/docstring/signature`，bm25 权重 `(0, 3, 2, 0.2, 1)` 逐字平移；过滤（kind/source_file）走元数据表 WHERE 先收窄再 JOIN FTS——codegraph 标准路径照搬 |
| camelCase 分段 | **做**（实测 TS 符号 camelCase 占比 75.6%，31/41，不属"小"）——方案 **(b) 双向预处理**：索引时 name/qualified_name 列文本 camel→空格拆段，查询词同转换；`snippet()` 只用于 docstring 列（ranked.py `snippet(nodes_fts,3,…)` 列 3），name 列无高亮失真风险 |
| FTS 索引范围 | **(b) 语义节点进 FTS**（graphify 差异化价值面：搜"attention mechanism"这类概念）；file 节点不进（文件名走 pinning / 元数据表 WHERE source_file LIKE——进 FTS 只会引入噪声：搜 utils 返回 50 个文件名淹没符号结果） |

### Schema

```
graphify-out/.fts-index.db（隐藏，事实层派生缓存，删除无损）
  ├─ nodes 元数据表（全量节点，含 file/语义节点——服务 02 票 17 直查点中的点查类）
  │    id / kind / name(label) / qualified_name / signature / docstring /
  │    source_file / source_location / end_line / end_byte / language
  ├─ nodes_fts FTS5 外部内容表（content='nodes', content_rowid='rowid'，对齐 codegraph）
  │    5 列：id / name / qualified_name / docstring / signature
  │    bm25(nodes_fts, 0, 3, 2, 0.2, 1) —— ranked.py 迁移零漂移（02 票等价性结论）
  └─ 元信息表：graph.json (mtime_ns, size) 指纹（serve 重启时判缓存可复用）
```

### 节点 → FTS 行映射规则（用户裁决，含实测修正）

| 节点类型 | FTS name 列 | FTS qualified_name 列 | FTS docstring 列 | FTS signature 列 |
| --- | --- | --- | --- | --- |
| AST 符号 | node.name（label） | node.qualified_name | node.docstring | node.signature |
| 语义概念（kind=None） | node.label | node.label（同值） | 空串（**实测修正**：486 个语义节点无 description 字段，字段集实测为 label/norm_label/source_url/author/contributor/captured_at 等；接口保留，未来语义提取产出 description 再接） | 空串 |
| file 节点 | 不进 FTS | — | — | — |

语义节点 label 双列同值 = BM25 权重 3+2=5 加成——合理：概念 label 本身就是高信号文本，被搜到时应排前。

### 外部内容表同步接口（用户细则，提前留口）

`content='nodes'` 模式要求 nodes 行变更时手动同步 FTS：`INSERT INTO nodes_fts(nodes_fts) VALUES('rebuild')`。首期**全量 rebuild**（整库删了重建，tmp + rename 原子替换）无所谓；但 watcher 后续若想做局部 FTS 更新（04 票触发链的增量优化）而非全量重投影，此同步机制提前留好接口。

### 构建与失效

- **构建**：`rebuild_fts(graph.json_path, out_db)`，万级符号秒级；serve 首次查询惰性触发（serve 无 --watch 模式也独立工作）。
- **失效（两级）**：① watcher 进程内直通（04 票触发链末端调本票接口）；② serve 重启时指纹对比（graph.json (mtime_ns, size) vs 元信息表）→ 不匹配惰性重建。与 02 票 db_fingerprint 新语义对齐：`FTS 缓存 mtime vs graph.json mtime`（freshness 指纹平移，serve.py:1584 `_derive_freshness`）。
- **camelCase 拆段函数**：索引侧与查询侧共用一个 `_split_identifier()`（camel 边界 + 下划线 + 数字边界 → 空格），双向转换保证 MATCH 语义一致。

### 实测注记

- TS/JS 符号 camelCase 75.6%（本仓库 41 个样本，TS 面小但方向明确；用户真实 TS 项目占比更高）。
- codegraph FTS 无自定义 tokenizer（默认 unicode61），无列权重之外的定制——等价重建只需列/权重/tokenizer 三同。
- 语义节点 docstring 列空串起步：bm25 的 doc=0.2 权重对空列无影响（空文档不参与该列打分）。
