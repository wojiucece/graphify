# 01: 提取契约全链走通（Python 单语言）

**What to build:** 对一个 Python fixture 文件跑完整提取管线后，graph.json 中该文件的符号节点携带六件套新字段——参数签名、`Class::method` 限定名、`L行:C列` 起点定位、结束行、结束字节——而文件节点保持行号-only 豁免形态。这是整个原生索引能力的 tracer bullet：最窄路径穿透提取层 → 构建层 → 产物落盘全链。

**Blocked by:** None (can start immediately).

**Status:** ready-for-agent

- [ ] Python fixture 的函数/方法节点带签名（参数+返回注解，名字不进签名；构造器单独抽取并与类关联）

- [ ] 类方法节点 qualified\_name 为 `Class::method` 形态；模块级函数为裸名；局部函数不进链

- [ ] 符号节点 source\_location 为 `L110:C5` 形态；文件节点保持 `L1`；边保持行号-only

- [ ] 符号节点带 end\_line 与 end\_byte 顶层整数字段

- [ ] 新字段经构建层无白名单透传进 graph.json（同输入下提取→构建→落盘可重复断言）

- [ ] 下游既有判定（AST 层识别、文件节点筛选）在新格式下全绿——既有提取/构建测试无一回归

