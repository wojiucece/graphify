## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

Rules:
- For codebase questions, first run `graphify query "<question>"` when graphify-out/graph.json exists. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).

## Fork 维护规则（CUSTOM，区别于上游 graphifyy）

### 通用（fork 专属环境 quirks）
- **graphify AST 盲区**：嵌在 Python 字符串里的 JS/JSON 代码不被二次解析（如 `_OPENCODE_PLUGIN_JS` 里的 JS 符号 `GraphifyPlugin`/`tool.execute.before` 不进图谱）；任意字符串字面量内容 graphify 也不索引——查"字符串内容/字符串内嵌代码"要回退 read/grep，graphify 只管符号定位
- **check-custom.sh 路径**：脚本不 cd 到 fork 目录，必须 `cd D:/code/graphify_fork && bash scripts/check-custom.sh` 跑（相对路径 `graphify/__main__.py` 在 `D:/code` 下找不到，会误报全部缺失）

### 升级上游
- **用 merge 策略**（非 rebase）：`git fetch upstream` -> `git merge upstream/v8 --no-edit`。保留 fork 定制提交 hash，可逆（`git reset --hard <备份tag>` 回退），fork 维护惯例
- **禁跑 `scripts/sync.sh`**--它不是重装脚本，是完整同步：含 `git rebase upstream/v8`（与 merge 策略冲突，会抹掉 merge commit）+ `git push --force-with-lease`（未授权 outward 操作）。跑它会破坏 fork
- 同步前打备份 tag：`git tag v8-custom-pre-sync-N v8-custom`，失败可秒回退
- 冲突时搜 `CUSTOM:` 标记找所有改动点
- **上游重构搬文件时，fork 定制常量（`_PROMPT_HOOK`、`_OPENCODE_PLUGIN_JS`）易丢**--迁移要逐常量对比内容，不能只看函数签名相同就判定被吸收（`_OPENCODE_PLUGIN_JS` 方案C 就是这样漏过一次）
- **升级前先验证 `graphify --version` 能跑**--venv 可能被清空（ModuleNotFoundError），重装 `uv tool install --editable ".[mcp,openai]" --force` 恢复
- **kill MCP server 进程树再重装**--`bash scripts/kill-graphify-server.sh` 或 `taskkill //PID <root> //T //F`，否则 Windows 锁 Scripts 目录"拒绝访问"
- **重装只跑** `uv tool install --editable ".[mcp,openai,sql]" --force`（按需加 sql/dm/terraform extras；不要跑 sync.sh）
- **命令分离**（上游 0.9.13 重构后）：`graphify install claude`（装用户级 skill `~/.claude/skills/`）vs `graphify claude install`（注入项目级 hooks `.claude/settings.json`，含 UserPromptSubmit）。要刷新某项目 hooks 在该项目目录跑后者
- **升级后跑 `bash scripts/check-custom.sh` 确认守护有效**（exit 0、无 ✗）。本次 0.9.20 同步发现 check-custom.sh grep 路径过时（查 `__main__.py` 但定制 0.9.13 已迁 `install.py`），5 个假阳性 ✗ 且 exit 0 使守护静默失效--已修路径 + fail-on-✗，但每次升级仍要跑一遍确认无假阳性
### 版本号约定
- `pyproject.toml` version 必须带 `+fork.N` 后缀（PEP 440 local version，区分上游 graphifyy）
- 上游升 0.9.8 时本地改 `0.9.8+fork.N`，不要去掉 `+fork`
- `uv.lock` 里 graphifyy version 不带 `+fork`（local version 不进 lock，正常现象），接受上游版本即可

### Hook 配置
- `_install_claude_hook`（graphify/install.py，0.9.13 从 __main__.py 迁入）的 PreToolUse 注入已注释--与 context-mode 的 Read/Bash hook 冲突，勿恢复。上游 0.9.20 #1986 把 PreToolUse 触发面扩到 `Bash|Grep`，双重触发更严重，禁用理由增强
- 上游 0.9.8 把 PreToolUse 注入从 `append(_SETTINGS_HOOK)` 重构成 `extend(_claude_pretooluse_hooks())`，fork 的禁用逻辑注释的是 extend 那行。上游 0.9.20 给 `_claude_pretooluse_hooks()` 加了 `strict` 参数（默认 False），所有旧调用点兼容，fork 禁用路径不受影响
- SessionStart/SessionEnd/PreCompact 是手动写在 `~/.claude/settings.json` 的，`graphify claude install` 不注册
- 三个生命周期脚本命名统一：`sessionstart-graphify-server.sh` / `sessionend-graphify-update.sh` / `precompact-graphify-update.sh`
- 改 hook 后跑 `bash scripts/check-custom.sh` 验证（双向检查：PreToolUse 该禁用 + UserPromptSubmit 该启用）

### Backend 锁定（AstronCodingPlan）
- `~/.graphify/providers.json` 配了 custom provider `AstronCodingPlan`（讯飞星火 MaaS，OpenAI 兼容 API，`env_key: apiKey`）
- `apiKey` 环境变量设了 + 不设其他标准 key → `detect_backend()` 自动选 AstronCodingPlan，不漂移
- 建图谱：`graphify extract . --backend AstronCodingPlan`（调 LLM，有费用）
- 查图谱（query/path/explain/prompt-hook）不调 LLM，纯词法 + 图遍历

### prompt-hook 引导句（三层分流，26 token）
- `prompt_hook.py:249` 的 note 是 fork 核心，每次 UserPromptSubmit 注入
- 当前文案：`graphify pre-query: symbols→file+line here; strings/patterns→grep; large output→sandbox not context.`
- 三层分流对应完整分工链：符号定位→graphify（给 file+line）/ 字符串模式→grep（graphify 不索引任意字符串）/ 大输出→sandbox 不进 context（context-mode 兜底）
- 写 `sandbox` 不写 `ctx_execute`——解耦，靠 SessionStart 注入的工具栈让模型自己关联，context-mode 卸载/改名 nudge 不过时
- 跟 PreToolUse nudge（已禁用）设计哲学统一：未来重新启用 PreToolUse 时，nudge 文案复用同一套分流逻辑，不会跟 prompt-hook 打架
- 改文案前先 `tiktoken` 验证 token 数 + 非 ASCII 字符（→ 是 1 token/id=52118，其他 Unicode 可能被拆成 3 byte token）
- 原方案 `graphify graph pre-query. Use directly, skip grep/read.`（13 token）留在 `prompt_hook.py` 注释里作回退参考，缺点是 `skip grep` 过度承诺

### OpenCode 引导句（三层分流，纯 ASCII，28 token）
- `__main__.py:1548` 的 `_OPENCODE_PLUGIN_JS` 里 echo 字符串，OpenCode `tool.execute.before` 首次 bash 触发一次（`reminded = true` 后不再触发）
- 当前文案：`[graphify] pre-query: symbols get file+line here; strings/patterns need grep; large output to sandbox not context.`
- 跟 prompt-hook 同一套三层分流哲学，但纯 ASCII（OpenCode TUI 渲染零风险，注释 1528 保证 plain words 安全；`→` 在 TUI 未验证故不用）
- echo 文案硬约束：双引号内禁 backticks/`$()`/裸双引号/反斜杠（触发 bash 命令替换）；分号在双引号内是字面字符安全，双引号外的 ` ; ` 才分隔命令
- 砍了原版的 `GRAPH_REPORT.md` 引导——hook 时机是 bash 前，该提醒分流不是读报告（时机错配）
- 改源码后要在 OpenCode 项目重跑 `graphify install` 重新生成 `.opencode/plugins/graphify.js`（跟 Claude Code 的 `graphify claude install` 同理）
- 原方案（52 token）留在 `__main__.py` 注释里作回退参考

### OpenCode after hook（方案 C，扩展触发面到 read/write/edit/glob/grep）
- `_OPENCODE_PLUGIN_JS` 里同时注册 `tool.execute.before`（bash）+ `tool.execute.after`（read/write/edit/glob/grep），一个 plugin 两个 key 并存
- **机制差异**：bash 走 before 拼 echo（执行前，能改 command）；read等走 after 改 `output.output`（执行后，不阻塞）。OpenCode API 的 before 对非 bash 工具没有 additionalContext 注入能力，逼出两套方案
- **注入位置**：after 改 `output.output`，实测注入在工具结果**包装层**（`<path>` 上方），不污染文件内容，模型识别为"包装信息"非文件内容
- **实测结论**（read/write/edit/glob/grep 五工具全绿）：注入生效 + 不污染 + 模型理解"本次工具 fine，下次符号查询走 graphify"
- **引导句**：`[graphify] next time run graphify query/explain/path first for symbols (file+line here). This tool was fine for the actual edit/string search. Large output to sandbox, not context.`
- **语气决策**：用 `run`（指令）非 `try`（建议）非 `MUST`（强制）。after 拦不住，MUST 会空喊削弱信用；try 偏软。run 是"指令但不强制"的中间档。`This tool was fine` 肯定本次工具调用，避免事后指责困惑
- **每工具首次触发**：`remindedAfter` Set 记录已提醒工具名，read 提醒过不影响 edit（覆盖每种工具首次使用）
- **fail-open guard**：`typeof output.output !== "string"` 时跳过（防崩），不阻塞工具
- **类型定义参考**：`D:/code/opencode/plugin-types/`（index.ts/tool.ts/README.md，扒自 anomalyco/opencode dev）
- **check-custom.sh 守护**：检查 before + after 都存在 + after 用 run 非 try + fail-open guard 存在（rebase 丢失 after 时告警，非 bash 工具会静默不提醒）

### graphify vs context-mode 分工框架（判断 PreToolUse 是否重启用）
- **graphify 解决"去哪找"（定位阶段）**：query/explain/path 返回子图，AST 提取的符号节点带 `loc=L<行>` 精确行号（`extract.py:1796`），文件级节点 `loc=L1` 是占位非真行号
- **context-mode 解决"怎么读"（提取阶段）**：大输出走 sandbox（ctx_execute/ctx_batch_execute），字节不进上下文
- **Read 场景：接力关系**——graphify 给行号 → Read offset 小段 → 输出小不触发 context-mode；graphify 的 `loc=` 越准，context-mode 越少被触发
- **grep 场景：竞争关系**——grep 的定位+提取是同一动作，两个 hook 抢主导权；graphify 只覆盖 A 类（符号查找），B/C/D 类（字符串/模式/验证）无能为力；`loc=` 行号优势在 grep 用不上（grep 要匹配行不是符号定义行）
- **冲突震中是 grep**：Read 侧冲突轻（行号让 graphify 跟 context-mode 接力），Bash/grep 侧冲突重
- **PreToolUse 禁用决策仍成立**：prompt-hook（1 次注入+带 body）已吸收 PreToolUse（N 次唠叨+只给指令）的核心功能且更优；PreToolUse 唯一增量价值是"长工具链中段反复提醒"，是假设性的，不值得其 token 成本 + 残余摩擦
- **未来若重新启用 PreToolUse**：可只启用 `Read|Glob` hook（冲突轻），不启用 `Bash` hook（冲突重）；nudge 文案用同一套三层分流逻辑，不写 MANDATORY/You MUST（避免跟 context-mode 抢主导权）

### 排查 `unknown command 'prompt-hook'`
- 根因：项目 .venv 装了上游版 graphifyy（无 prompt-hook），激活后遮蔽全局 fork 版
- 排查：`bash scripts/check-custom.sh` 扫描所有 graphify.exe 标出上游版
- 清理：`uv pip uninstall graphifyy --python <venv>/Scripts/python.exe`
