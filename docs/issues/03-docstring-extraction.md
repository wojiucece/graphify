# 03: docstring 提取（Python / JS / TS）

**What to build:** 函数/方法/类/模块节点的 body 首语句字符串（Python docstring、JSDoc 块注释）作为 docstring 字段进入产物：原文存储、上限 1500 字符、[1350, 1500] 窗口内按段落>行>句优先级找断点、截断追加 `…[+N chars truncated]` 量化标记、strip 后不足 5 字符存 null。Python 走既有停留点回填；JS/TS 新写声明前注释关联。

**Blocked by:** 01（提取契约全链走通（Python 单语言））

**Status:** ready-for-agent

- [ ] Python fixture 的带 docstring 符号在 graph.json 携带原文 docstring
- [ ] 超 1500 的 docstring 在窗口内语义边界截断且带量化标记；不足 5 字符存 null
- [ ] 首语句判定精确：仅 body 首子语句且内部恰为单个字符串节点（无拼接、无 f/b/r 前缀）
- [ ] JS/TS 的 JSDoc 块关联到紧随其后的函数/类节点
- [ ] 既有 rationale 节点行为不受影响（语义面保持不动）
