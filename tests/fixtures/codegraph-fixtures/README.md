# mini.codegraph.db（测试 fixture）

- 生成方式：由 `codegraph init -y`（codegraph 1.6.0）对 3 个微型 Python 文件
  （`a.py` / `b.py` / `c.py`，含跨文件 import 与调用）索引后复制而来。
- schema 版本：9（`schema_versions` 表 `MAX(version)`）。
- 内容规模：8 nodes / 9 edges（AST 符号 + 跨文件引用）。
- 用途：供 adapter / orchestrator 测试读取真实 codegraph DB（只读校验、schema 探测、
  边/节点遍历）。生产 codegraph DB 的缩小样本，保持 schema v9 真实格式。
- 注意：源码目录 `fixture-src/` 已删除，仅保留 DB 本身。
