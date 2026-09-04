# 提取字段契约设计

label: wayfinder:grilling
status: closed
assigned: 2026-09-02（本会话）
blocked-by: \[01-extraction-audit.md]

## Question

三类新字段的精确形态（HITL，与用户 grill 后定）：

- signature 存什么：参数名+类型注解的完整签名文本，还是截断形式？类/字段/变量节点是否适用？

- docstring 摘要：长度上限、截断规则、哪些 kind 适用；语义节点（kind=None）明确排除

- 列定位格式：`L110:C5` 对下游 `^L\d` 正则判定的兼容性（01 票盘点结论为输入）

- 字段落点：graph.json 节点属性向后兼容新增，还是独立 sidecar（倾向前者，受"原有能力保留"约束）

- 是否回传上游 PR 的前置判断（影响字段命名与实现位置的取舍）

## Resolution

裁决（2026-09-02 用户全采纳 + 实现级细则；契约基准：本仓库 `.codegraph/codegraph.db` 实测 schema + vendored codegraph v1.5.0 源码）：

### 字段契约总表

| 字段                | 形态                                                                                                     | 适用 kind                                                           | 细则                                                                               |
| ----------------- | ------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------- | -------------------------------------------------------------------------------- |
| `signature`       | 按 kind 语义化，**名字不进签名**：函数/方法 = `(a: int, b: str = "x") -> bool`；变量/常量 = `= 右侧表达式`；import 与 class **不给** | function/method（含 `__init__`/constructor）/variable/field/constant | 见细则 A1–A4                                                                        |
| `docstring`       | 原文存储，上限 **1500 字符 + 智能截断 + 量化超限标记**（v1.2 修正案：上限 1000→1500，over-limit 降至 0.86%，见细则 B2 与数据注记）            | function/method/class/module（有 body 首语句 string 的）                 | 见细则 B1–B3                                                                        |
| `qualified_name`  | `Class::method` 作用域链                                                                                   | 全部 AST 符号                                                         | **三条铁律**：模块级符号无前缀（裸名）；局部函数/嵌套函数不进链（保持裸名）；与 nid 并存（qualified\_name 是可读寻址，nid 是身份） |
| `source_location` | 符号节点 `L110:C5`（起点）；文件节点保持 `L1` 豁免；边不加列                                                                 | 全部 AST 符号                                                         | 01 票已验证 12 处下游消费点全兼容                                                             |
| `end_line`        | 顶层整数字段                                                                                                 | 全部 AST 符号                                                         | serve 现有切片工具的兼容原语（06 票迁移用）                                                       |
| `end_byte`        | 顶层整数字段                                                                                                 | 全部 AST 符号                                                         | **精确切片的正确原语**（tree-sitter 节点天然有 end\_byte/end\_point.row，抽取时多赋一个字段即可）            |

### A. signature 实现细则（用户裁决）

1. **变量三条规则**：有 `=` → 取 `=` 右侧节点的 source text，压平 + 截断 100；无 `=` 但有类型注解 → `signature = null`（类型信息已在 type 相关字段，别重复）；截断**按 token/节点边界断，不硬切字符串中间**。
2. **函数签名跳过 name 子节点**：tree-sitter 各语言 grammar 字段名不同（`parameters`/`params`/`parameter`…），**按语言声明字段名，别假设通用**——参考实现：vendored `codegraph/src/extraction/languages/python.ts:19` 的 `getSignature` 钩子（`getChildByField(node, 'parameters')` + `->` + `return_type`）与 `tree-sitter-types.ts` 的 LanguageExtractor 接口（`nameField/paramsField/returnField` 每语言声明）。
3. **`__init__`/constructor 要给签名**：作为 method kind 单独抽取、signature 正常存 `(name: str, age: int = 0) -> None`；搜索/前端把 class 与其 constructor signature 关联展示（`UserService(name: str)` 可命中）。参考：tree-sitter.ts `initSignature` 多分支（L2600–2848）。
4. **截断标记**：截断后必须加标记（如尾部 `…` 或独立布尔字段），让下游（LLM/人类）知道不完整，**不许默默截断**——此处比 codegraph 做得好（它 `slice(0, 80)` 硬切无标记，tree-sitter.ts:2325）。

### B. docstring 实现细则（用户裁决）

1. **首语句判定精确化**：只取函数/类/模块 body 的 `children[0]`，且该 child 是 expression\_statement 且内部**仅含一个 string 节点**（无拼接、无 f/b/r 前缀）。
2. **智能截断 + 量化超限标记**（v1.2 修正案，上限 1000→1500）：超 1500 字符时，在 **\[1350, 1500] 窗口**（上限前 150 字符）内按优先级找断点——段落边界（`\n\n`）> 行边界（`\n`）> 句边界（`。.!?` + 空白）；窗口内无任何边界才硬切 1500。截断后追加**量化标记**  ` …[+N chars truncated]`（N = 原长 − 断点位置），下游 LLM/人类既知道不完整、又知道丢了多少。Unicode-safe：Python 字符串切片本身不断在 UTF-8 编码中间。
3. **噪声过滤**：strip 后长度 < 5 字符的 docstring 存 null，避免索引噪声。

**数据注记（v1.1 二轮实测 9278 个 docstring；v1.2 据此定上限）**：超限分布窄带集中已验证（over-1000 = 247 个 / 2.66%，其中 1000–1500 窄带占 68%）；各上限实测 over-limit：1200 → 1.67%、1500 → **0.86%**（247→80 个，增量存储 \~123KB ≈ graph.json 体积 1%）。v1.2 裁决：上限 1500，基本消除截断面（99.14% 完整），存储代价可忽略。**智能截断 trade-off 实测**：断点回退使平均损失从硬切 452 升至 503 字符（+11%）——买的是语义完整断点（不截在单词/句子中间），不是损失减少；接受此交换（1500 上限下此 trade-off 仅作用于 0.86% 的节点）。

### C. 范围与策略

- **首批语言覆盖 = 方案 A**：engine.py 15 个 LanguageConfig（signature/qualified\_name/列/end\_line/end\_byte 全量）+ docstring 仅 Python/JS/TS；Go/Rust 与 26 个专用提取器（方案 B）押后到 FTS 实战（05/06 票）验证检索价值后再扩。改动量估计：01 票 95–130 行 + qualified\_name \~15 行 + end\_line/end\_byte \~10 行 ≈ **120–155 行**。

- **上游 PR 策略**：本地补丁先行（字段可能随 FTS 实战调整形态），代码集中（`_signature_head` helper + `add_node` 参数化）为 PR 留路不封死。

- **字段落点**：节点**顶层字段**（build.py:976 `G.add_node(id, **node)` 无白名单透传；不走 metadata——会被 `sanitize_metadata` 截断转义）。rationale 节点保留不动（图语义用途，docstring 字段纯新增）。

### 参考实现指针（vendored codegraph v1.5.0，无需拉 1.6）

1.6 的主要变化是 WAL 修复（静置上限 64MB），与提取逻辑无关，vendored v1.5.0 足够参考；若后续发现 1.5↔1.6 提取逻辑差异再拉上游。关键文件：`codegraph/src/extraction/languages/<lang>.ts`（每语言 getSignature/getDocstring 钩子）、`codegraph/src/extraction/tree-sitter.ts`（通用层 createNode/initSignature/变量截断）、`codegraph/src/extraction/tree-sitter-types.ts`（LanguageExtractor 接口）。**抄设计（钩子模式/字段名声明），不抄代码**（TS→Python 移植，且 MIT 署名纪律沿用 fork 惯例）。
