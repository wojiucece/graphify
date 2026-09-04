# 提取层增强点盘点（signature / docstring / 列定位改在哪、改多少）

label: wayfinder:research
status: closed
blocked-by: \[]

## Question

在 graphify fork 的 AST 提取层为符号节点补齐三类字段——① signature（函数/方法/类签名）② docstring/文档注释摘要 ③ 列定位（`start_point[1]`）——的具体改动点在哪里、每个点改动量多大？

已确认事实：[engine.py:1512](../../../graphify/extractors/engine.py) 等处 `source_location: f"L{node.start_point[0]+1}"` 丢弃了列；本仓库 graph.json 11606 节点中 0 个含 docstring/signature。

需盘点：

1. engine.py 及各语言提取器中共多少处符号节点 dict 构造点（往 nodes append symbol 的位置），语言间是否共用单一构造函数（若共用则改一处全语言受益）
2. 列信息：各构造点是否都能拿到 tree-sitter node（start\_point 元组）
3. signature：构造点处能否用 node 文本切片低成本拼出签名
4. docstring：Python/JS/TS 等提取器现在是否路过 docstring/文档注释（拿到没存）；哪些语言有现成停留点、哪些要新写 tree-sitter 查询
5. source\_location 格式变化（`L110` → `L110:C5`）的下游消费点：grep 所有 `^L\d` / source\_location 判定处（如 build.py 的 `_is_ast_tier`），确认新格式是否兼容
6. 每处改动行数估计与总改动量

产出：改动点清单表 + 每点估计 + 下游兼容点清单。

## Resolution

研究完成（2026-09-02，只读盘点，未改源码）。

### 结论速览

1. **无全语言单一工厂，是"1 + 26"双轨**：engine.py `_extract_generic` 的 `add_node` 闭包是 15 个 LanguageConfig（Python/JS/TS/TSX/Java/Groovy/C/C++/Ruby/C#/Kotlin/Scala/PHP/Lua/Swift）共用的唯一构造函数——**改这一处，主流语言全受益**；另 26 个专用提取器各持独立 `add_node` 闭包（27 处 def，dict 字面量内联，无共享设施）。
2. **列信息全可达**：engine 所有符号 `add_node` 调用点均在 tree-sitter `node` 作用域内（`node.start_point[1]` 直接可取）；专用提取器闭包签名只收 `line: int`，但调用方持 node，需改闭包签名传列。注：ticket 所引 engine.py:1512 实为边构造（`_dynamic_import_js`），真正的符号构造点是 `add_node` @ engine.py:3004。
3. **signature 可低成本拼出**：`body = _find_body(node, config)`（engine.py:1442 已有）→ `source[node.start_byte:body.start_byte]` 文本切片 → `" ".join(split())` 压平 → 截断。serve.py `_signature_line`（serve.py:2823，消费 codegraph DB 的无 def 前缀签名）已证明该形态可用。
4. **docstring 停留点：Python/CL 有，JS/TS/Go/Rust 无**（详见下表）。Python 的 `_extract_python_rationale` 后处理（extract.py:1185）已有完整 `_get_docstring`（extract.py:1207），nid 计算与符号节点同构，回填符号节点仅 \~10 行。
5. **`L110:C5`** **兼容性：风险点仅 3 文件 5 处精确** **`== "L1"`**（均文件级节点判定）；只要文件节点保持纯 `L1`、只给符号节点加列、边不加列，则全部兼容，`^L\d` / `re.match(r"L(\d+)")` 均为前缀匹配不受影响。
6. **总改动量：方案 A（engine 路径 + Python/JS/TS docstring）约 110–130 行；方案 B（再覆盖 26 个专用提取器）约 350–450 行。**

### 1. 符号节点构造点清单

**A 轨：engine.py** **`_extract_generic`（覆盖 15 个 LanguageConfig 语言）— 单点改造**

| 位置                           | 符号类型                    | 所在函数                                    | node 可达          |
| ---------------------------- | ----------------------- | --------------------------------------- | ---------------- |
| engine.py:3004-3025          | **构造函数本体**（唯一 dict 工厂）  | `_extract_generic`.add\_node            | —                |
| engine.py:3152               | class/interface         | `_extract_generic`.walk                 | ✓                |
| engine.py:4232 / 4236        | 方法 / 函数                 | `_extract_generic`.walk                 | ✓                |
| engine.py:3896               | 类 property（TS 等）        | `_extract_generic`.walk                 | ✓                |
| engine.py:4089               | static property         | `_extract_generic`.walk                 | ✓                |
| engine.py:4190               | C/C++ 字段                | `_extract_generic`.walk（decl）           | ✓                |
| engine.py:4615 / 4624 / 4714 | JS object literal 属性/方法 | `_extract_generic`.walk                 | ✓                |
| engine.py:2048               | JS 嵌套函数                 | `_scan_js_nested_function_declarations` | ✓（child）         |
| engine.py:2829               | Ruby Struct/Class 工厂类   | `_ruby_extra_walk`                      | ✓                |
| engine.py:2501 / 2524        | C# namespace            | C# extra-walk helper                    | ✓                |
| engine.py:3071               | 文件节点（line=1）            | `_extract_generic`                      | ✓（建议保持 `L1` 不加列） |

engine.py 内 7 处直接 `nodes.append({...})`（2853/3060/3093/3213/3253/3415/3468）均为 source\_file="" 的 stub 或 module 占位节点，**不适用**三字段（无源文本）。

**B 轨：26 个专用提取器独立闭包（无共享工厂）**

| 构造点（file:line）                  | 提取函数                                                | node 可达                            |
| ------------------------------- | --------------------------------------------------- | ---------------------------------- |
| apex.py:26                      | extract\_apex                                       | ✓（tree-sitter）                     |
| bash.py:140                     | extract\_bash                                       | ✓                                  |
| commonlisp.py:116               | extract\_commonlisp                                 | ✓                                  |
| dart.py:58                      | extract\_dart                                       | ✗（正则+剥注释，现状 source\_location=None） |
| dm.py:34                        | extract\_dm                                         | ✓                                  |
| elixir.py:34                    | extract\_elixir                                     | ✓                                  |
| fortran.py:74                   | extract\_fortran                                    | ✓                                  |
| go.py:114                       | extract\_go                                         | ✓                                  |
| json\_config.py:94              | extract\_json                                       | ✗（JSON 无列概念，可给 key 列）              |
| julia.py:33                     | extract\_julia                                      | ✓                                  |
| markdown.py:288                 | extract\_markdown                                   | 半（逐行正则，列=match.start() 可算）         |
| objc.py:118                     | extract\_objc                                       | ✓                                  |
| ocaml.py:55                     | extract\_ocaml                                      | ✓                                  |
| pascal.py:248（\_add\_node）/ 478 | \_extract\_pascal\_regex / 表单元数据                    | ✗（纯正则，列可从 match.start(line) 算）     |
| pascal\_forms.py:45 / 144       | extract\_delphi\_form / extract\_lazarus\_form      | ✗（表单文本）                            |
| powershell.py:35 / 410          | extract\_powershell / extract\_powershell\_manifest | ✓ / ✗                              |
| robot.py:126                    | extract\_robot                                      | ✗（逐行）                              |
| rust.py:85                      | extract\_rust                                       | ✓                                  |
| sql.py:322（\_add\_node）         | extract\_sql                                        | ✗（掩码+正则，列可算）                       |
| terraform.py:56（\_add\_node）    | extract\_terraform                                  | ✗（逐行）                              |
| verilog.py:117 / 230            | extract\_verilog（两个函数）                              | 半（正则）                              |
| zig.py:34                       | extract\_zig                                        | ✓                                  |

另有非 tree-sitter 的 blade.py / razor.py / sln.py（source\_location=None 或无字段）不适用。**结论：tree-sitter 类 13 个提取器列信息天然可达；正则类 8 个需从 match 偏移折算列。**

### 2. signature 拼接方式（低成本，\~35 行共享 helper）

- **engine 路径**：新增模块级 `_signature_head(node, source, config)`：`_find_body()` 有 body → 取 `source[node.start_byte:body.start_byte]`；无 body（TS interface 方法、C++ 声明）→ 回退 `child_by_field_name("parameters")` 文本；`" ".join(text.split())` 压平多行 + 截断（如 200 字符；注意 `sanitize_metadata`（security.py:441）对长字符串有截断，签名走顶层字段或确认截断上限）。在 `add_node` 加 `signature: str | None = None` 参数写入节点。

- **专用提取器**：go/rust 等在符号处理处取 body child 同法；正则类（pascal/sql）在 match 处截原文。

- **下游先例**：serve.py:2819-2823 `Signature: {_signature_line(sig, short)}`——codegraph DB 存的就是无 `def` 前缀签名行，格式可直接对齐。

### 3. docstring / 文档注释停留点

| 语言                                                                      | 停留点                                                                             | 现状                                                                                                                                       | 改造                                                                      |
| ----------------------------------------------------------------------- | ------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------- |
| Python                                                                  | **有**：extract.py:1207 `_get_docstring`（`_extract_python_rationale` 后处理）         | 已完整提取 body 首语句 string（>20 字符），但只生成独立 rationale 节点，未存符号节点；其 nid 与符号节点同构（`_make_id(stem, class_name)` / `_make_id(parent_nid, func_name)`） | **回填 \~10 行**：post-pass 中向 `result["nodes"]` 对应符号 dict 写 `docstring` 字段 |
| CommonLisp                                                              | **有**：commonlisp.py:327-336、414-426                                             | doc\_text 已取出 → rationale 节点                                                                                                             | 同 Python 回填                                                             |
| JS/TS                                                                   | **无符号级**：extract.py:1507 `_extract_js_rationale` 仅逐行 `// NOTE:` 前缀 + ADR/RFC 引用 | JSDoc 块（`/** */`）完全未解析、不关联符号                                                                                                             | **新写 \~30 行**：tree-sitter 中函数/类声明 `prev_sibling` 为 `/**` 开头 comment 即取  |
| Go                                                                      | **无**                                                                           | comment 节点从未被访问（tree-sitter-go 无 doc field，doc 为声明前兄弟 comment）                                                                           | 新写 \~15 行（prev\_sibling 扫描）                                             |
| Rust                                                                    | **无**                                                                           | `///` 为 line\_comment 节点，提取器不读                                                                                                           | 新写 \~15 行                                                               |
| 其余（objc/dm/bash/powershell/julia/fortran/ocaml/zig/elixir/apex/pascal…） | **无**                                                                           | 无任何 comment 提取（comment 相关代码全部是"剥除/掩码"用途：pascal.py:77、dart.py:18、verilog.py:61、sql.py:79）                                                 | 按语言新写                                                                   |

### 4. source\_location 格式变化（`L110` → `L110:C5`）下游消费点全表

前提约定：**仅符号节点加列；文件节点保持** **`L1`；边不加列**（add\_edge 不动）。

| #  | 消费点                                                                                                                         | 判定方式                                                           | 兼容性                                   |
| -- | --------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------- | ------------------------------------- |
| 1  | build.py:41,52 `_AST_LOC_RE = ^L\d` / `_is_ast_tier`                                                                        | 前缀正则                                                           | ✓ `L110:C5` 仍匹配                       |
| 2  | build.py:730 `graph_has_legacy_ids`                                                                                         | `!= "L1"`（文件节点筛选）                                              | ✓ 文件节点保持 L1 即无影响                      |
| 3  | build.py:1014                                                                                                               | 真值判定                                                           | ✓                                     |
| 4  | affected.py:113,122 `_prefer_file_node`                                                                                     | `== "L1"`（文件节点）                                                | ✓ 同上                                  |
| 5  | serve.py:1334 节点搜索排序                                                                                                        | `!= "L1"`（文件节点）                                                | ✓ 同上                                  |
| 6  | extract.py:4870 C# event handler 签名检测                                                                                       | `re.match(r"L(\d+)")`                                          | ✓ 前缀捕获，group(1)=110                   |
| 7  | engine.py:26 `_source_location`                                                                                             | `startswith("L")` 透传                                           | ✓                                     |
| 8  | extract.py:2819-2821 半同节点折叠排序                                                                                               | 字符串比较 key                                                      | ✓ 排序语义单调                              |
| 9  | export.py:793（YAML）、benchmark.py:67、cli.py:1746、diagnostics.py:224                                                          | 展示/透传                                                          | ✓ 仅变长                                 |
| 10 | dedup.py:418                                                                                                                | None 判定                                                        | ✓                                     |
| 11 | scripts/adapter.py:79                                                                                                       | **生产侧**（外部 codegraph.db→graphify，自产 `L{line}`，注释明示判层依赖 `^L\d`） | 不受影响；若未来 adapter 加列须保持 `L` 开头         |
| 12 | tests：test\_build.py:1258/1374（`^L\d` 语义）、test\_extract.py:3650（文件节点 `=="L1"`）、test\_kotlin\_grammar.py:408（**边** `==L{n}`） | 精确断言                                                           | 边不加列则不破；符号节点精确断言需跑测试筛查，预估 5–10 处需同步更新 |

其余 `source_location` 出现点均为**生产者**（scip\_ingest / mcp\_ingest / cargo\_introspect / manifest\_ingest / detect），自产格式不受提取器改动影响。

### 5. 改动量估计

| 改动项                                                                                 | 位置                                     | 行数估计          |
| ----------------------------------------------------------------------------------- | -------------------------------------- | ------------- |
| add\_node 闭包改造（+col/signature/docstring 参数，location 拼列）                             | engine.py:3004                         | \~12          |
| `_signature_head` 通用 helper                                                         | engine.py 新增                           | \~20          |
| walk/helper 调用点传参（3152/4232/4236/3896/4089/4190/4615/4624/4714/2048/2829/2501/2524） | engine.py ×13 处                        | \~20          |
| Python docstring 回填符号节点                                                             | extract.py `_extract_python_rationale` | \~10          |
| JS/TS JSDoc 关联新写                                                                    | engine.py 或 extract.py                 | \~30          |
| 文件节点豁免列（file\_nid 用 line-only 分支）                                                   | engine.py:3071                         | \~2           |
| **方案 A 小计（engine 路径 + Py/JS/TS）**                                                   | engine.py + extract.py                 | **\~95–130**  |
| 26 个专用提取器闭包加列（每闭包 +2-3 行 ×27 + 调用点 ×1 行 ×\~110）                                     | extractors/\*.py                       | \~180–250     |
| Go/Rust 等专用提取器 docstring                                                            | extractors/go.py、rust.py 等             | 每语言 10–20     |
| 测试适配（精确断言更新）                                                                        | tests/                                 | \~10–30       |
| **方案 B 小计（全语言覆盖）**                                                                  | <br />                                 | **\~350–450** |

### 风险与建议

- **唯一格式风险**：build.py:730 / affected.py:113,122 / serve.py:1334 的精确 `== "L1"` 依赖文件节点不带列——实现时文件节点必须豁免（engine.py:3071 及各专用提取器 `add_node(file_nid, path.name, 1)` 调用）。

- signature/docstring 走节点**顶层字段**最省事（build.py:976 `G.add_node(id, **node)` 无字段白名单，任意顶层字段直接透传进图）；若走 `metadata` 需注意 `sanitize_metadata` 的长度截断与 HTML 转义。

- 建议先落方案 A（改 2 个文件覆盖 15 config 语言，含 Python/JS/TS/Java/C#/Go 主流大部分场景），B 轨按语言渐进。

