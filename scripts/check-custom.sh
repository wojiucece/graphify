set -euo pipefail
echo "=== 你的自定义改动（CUSTOM 标记）==="
git grep -l "CUSTOM:" || echo "(无标记改动)"
echo ""
echo "=== 新增文件 ==="
git diff --name-status upstream/v8...HEAD | grep "^A" | awk '{print $2}' || true
echo ""
echo "=== 修改的上游文件 ==="
git diff --name-status upstream/v8...HEAD | grep "^M" | awk '{print $2}' || true
echo ""
echo "=== 新增文件存在性检查 ==="
for f in graphify/prompt_hook.py scripts/sync.sh scripts/sessionstart-graphify-server.sh scripts/precompact-graphify-update.sh scripts/sessionend-graphify-update.sh scripts/check-custom.sh; do
    [ -f "$f" ] && echo "✓ $f" || echo "✗ $f 缺失"
done

echo ""
echo "=== PreToolUse 注入禁用检查（避免与 context-mode 的 Read/Bash hook 冲突）==="
# _install_claude_hook 中 PreToolUse 注入应被注释掉，只保留 UserPromptSubmit
# 上游 0.9.8 重构：PreToolUse 注入从 append(_SETTINGS_HOOK) 改为 extend(_claude_pretooluse_hooks())
# 恢复方式：取消 _install_claude_hook 函数中被注释的 extend(_claude_pretooluse_hooks()) 行
if grep -q '^    # hooks\["PreToolUse"\].extend(_claude_pretooluse_hooks())' graphify/__main__.py 2>/dev/null; then
    echo "✓ PreToolUse 注入已禁用（_install_claude_hook 中 _claude_pretooluse_hooks() extend 被注释）"
else
    echo "✗ PreToolUse 注入未禁用：graphify/__main__.py 中 _claude_pretooluse_hooks() extend 未被注释"
    echo "  应在 _install_claude_hook 函数中注释掉 hooks[\"PreToolUse\"].extend(_claude_pretooluse_hooks()) 行（与 context-mode 冲突）"
fi

echo ""
echo "=== UserPromptSubmit 注入检查（prompt-hook 是 fork 核心，必须启用）==="
# _install_claude_hook 中 UserPromptSubmit 注入必须存在且未注释
# rebase 时若这段被上游覆盖，prompt-hook 失效但其他检查全绿——隐性丢功能
if grep -q '^    hooks\["UserPromptSubmit"\].append(_PROMPT_HOOK)' graphify/__main__.py 2>/dev/null; then
    echo "✓ UserPromptSubmit 注入存在（_PROMPT_HOOK append 未注释，prompt-hook 生效）"
else
    echo "✗ UserPromptSubmit 注入缺失：graphify/__main__.py 中 _PROMPT_HOOK append 未找到或被注释"
    echo "  应在 _install_claude_hook 函数中保留 hooks[\"UserPromptSubmit\"].append(_PROMPT_HOOK)（CUSTOM: add UserPromptSubmit hook 段）"
fi

echo ""
echo "=== Fork 版本号检查（区分本地 fork 与上游 graphifyy）==="
# pyproject.toml 中 version 应带 +fork 后缀，区分本地构建与上游发布版
# _version_tuple 解析 "0.9.5+fork.1" → (0,9,5,1) > 上游 (0,9,5)，版本比较正确
FORK_VER=$(grep '^version = ' pyproject.toml | head -1 | sed 's/^version = "\([^"]*\)".*/\1/')
case "$FORK_VER" in
    *+fork*)
        echo "✓ Fork 版本号标识存在: $FORK_VER"
        ;;
    *)
        echo "✗ Fork 版本号标识缺失：pyproject.toml 中 version 应带 +fork 后缀（当前: $FORK_VER）"
        echo "  例: version = \"0.9.5+fork.1\""
        ;;
esac

echo ""
echo "=== graphify 安装位置检查（确保所有 graphify.exe 都是 fork 版）==="
# 扫描所有 graphify.exe，发现上游版（无 +fork 后缀）就警告
# 防止 venv 或全局被上游 graphifyy 污染（上游版无 prompt-hook 命令）
# 升级 fork 的正确方式：uv tool install --editable D:/code/graphify_fork --force
# 错误方式（会拉上游版）：uv tool upgrade graphifyy / pip install -U graphifyy
GRAPHIFY_FOUND=0
while IFS= read -r f; do
    GRAPHIFY_FOUND=1
    ver=$("$f" --version 2>&1 | head -1)
    case "$ver" in
        *+fork*)
            echo "✓ $f → $ver"
            ;;
        *)
            echo "✗ $f → $ver（上游版！无 prompt-hook 命令）"
            # 判断是 venv 还是全局，给出对应清理建议
            case "$f" in
                */.venv/Scripts/graphify.exe|*/.venv/bin/graphify)
                    venv_python="$(dirname "$(dirname "$f")")/python.exe"
                    echo "  清理: uv pip uninstall graphifyy --python $venv_python"
                    ;;
                *)
                    echo "  重装 fork: uv tool install --editable D:/code/graphify_fork --force"
                    ;;
            esac
            ;;
    esac
done < <(find /d -maxdepth 6 -name "graphify.exe" 2>/dev/null)
[ "$GRAPHIFY_FOUND" = "0" ] && echo "✗ 未找到任何 graphify.exe（可能未安装）"

# 全局命令版本（PATH 解析到的）
global_ver=$(graphify --version 2>&1 | head -1)
case "$global_ver" in
    *+fork*) echo "✓ 全局 graphify（PATH 解析）→ $global_ver" ;;
    *) echo "✗ 全局 graphify（PATH 解析）→ $global_ver（上游版！重装: uv tool install --editable D:/code/graphify_fork --force）" ;;
esac

echo ""
echo "=== 全局 hook 配置检查（~/.claude/settings.json 的生命周期 hook）==="
SETTINGS="$HOME/.claude/settings.json"
if [ -f "$SETTINGS" ]; then
    checks=(
        "SessionStart:sessionstart-graphify-server.sh:sessionstart-graphify-server.sh"
        "SessionEnd:sessionend-graphify-update.sh:sessionend-graphify-update.sh"
        "PreCompact:precompact-graphify-update.sh:precompact-graphify-update.sh"
    )
    all_ok=0
    for check in "${checks[@]}"; do
        IFS=':' read -r event script_name pattern <<< "$check"
        if grep -q "$pattern" "$SETTINGS" 2>/dev/null; then
            echo "✓ $event → $script_name 已注册"
        else
            echo "✗ $event → $script_name 未注册（需手动添加到 ~/.claude/settings.json 的 $event hook）"
            all_ok=1
        fi
    done
    [ "$all_ok" = "0" ] && echo "  所有生命周期 hook 完整"
else
    echo "✗ ~/.claude/settings.json 不存在"
fi

echo ""
echo "=== OpenCode hook 检查（_OPENCODE_PLUGIN_JS 的 before + after）==="
# OpenCode 走 _OPENCODE_PLUGIN_JS（__main__.py）注册 tool.execute.before/after
# before：bash 拼 echo（执行前提醒）；after：read/write/edit/glob/grep 改 output.output（执行后提醒）
# after 是方案 C 新增，rebase 时若被上游覆盖，OpenCode 非 bash 工具不提醒——隐性丢功能
MAIN_PY="graphify/__main__.py"
if grep -q '"tool.execute.before"' "$MAIN_PY" 2>/dev/null; then
    echo "✓ OpenCode before hook 存在（bash 拼 echo）"
else
    echo "✗ OpenCode before hook 缺失：$MAIN_PY 中 tool.execute.before 未找到"
fi
if grep -q '"tool.execute.after"' "$MAIN_PY" 2>/dev/null; then
    echo "✓ OpenCode after hook 存在（read/write/edit/glob/grep 改 output.output）"
else
    echo "✗ OpenCode after hook 缺失：$MAIN_PY 中 tool.execute.after 未找到（方案 C 丢失，非 bash 工具不提醒）"
fi
# after 引导句语气检查：用 run（指令）非 try（建议）
if grep -q 'next time run graphify' "$MAIN_PY" 2>/dev/null; then
    echo "✓ after 引导句用 run（指令语气）"
elif grep -q 'next time try graphify' "$MAIN_PY" 2>/dev/null; then
    echo "✗ after 引导句用 try（建议语气偏软，应改 run）"
fi
# after fail-open guard 检查
if grep -q 'typeof output.output !== "string"' "$MAIN_PY" 2>/dev/null; then
    echo "✓ after fail-open guard 存在（output.output 非 string 时跳过，防崩）"
else
    echo "✗ after fail-open guard 缺失（output.output 非 string 时会崩）"
fi
