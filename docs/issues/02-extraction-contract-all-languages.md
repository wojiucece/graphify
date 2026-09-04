# 02: 提取契约扩展 15 语言

**What to build:** 把 01 在 Python 走通的六件套字段扩展到通用提取层支持的全部 15 个语言配置（Java/C#/JS/TS/TSX/Groovy/C/C++/Ruby/Kotlin/Scala/PHP/Lua/Swift 等）：每种语言按其 tree-sitter grammar 声明自己的字段名（参数字段、返回类型字段等），不假设跨语言通用。变量/常量节点按三条规则给 `=` 右侧头部签名（有等号取右侧压平截断、仅类型注解则 null、按 token 边界断并加截断标记）。

**Blocked by:** 01（提取契约全链走通（Python 单语言））

**Status:** ready-for-agent

- [ ] 15 个语言配置的 fixture 抽样符号节点六件套字段齐全（每语言至少一个函数/方法签名断言）

- [ ] 变量签名遵守三条规则；截断处带不完整标记

- [ ] 各语言 grammar 字段名以语言配置声明，无跨语言硬编码

- [ ] 既有全语言提取测试无回归

## Resolution 指针

精确实现坐标（15 语言 LanguageConfig + 变量签名三条规则）见 wayfinder 决策票
[`tickets/03-field-contract.md`](../wayfinder/tickets/03-field-contract.md) 的 Resolution 段；
本票为验收清单，不携带实现坐标。

