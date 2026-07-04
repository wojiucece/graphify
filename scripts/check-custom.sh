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
for f in graphify/prompt_hook.py scripts/sync.sh scripts/start-graphify-server.sh scripts/stop-graphify-server.sh scripts/precompact-graphify-update.sh scripts/sessionend-graphify-update.sh scripts/check-custom.sh; do
    [ -f "$f" ] && echo "✓ $f" || echo "✗ $f 缺失"
done

echo ""
echo "=== PreToolUse 注入禁用检查（避免与 context-mode 的 Read/Bash hook 冲突）==="
# _install_claude_hook 中 PreToolUse 注入应被注释掉，只保留 UserPromptSubmit
# 恢复方式：取消 _install_claude_hook 函数中被注释的 PreToolUse 四行
if grep -q '^    # hooks\["PreToolUse"\].append(_SETTINGS_HOOK)' graphify/__main__.py 2>/dev/null; then
    echo "✓ PreToolUse 注入已禁用（_install_claude_hook 中 _SETTINGS_HOOK append 被注释）"
else
    echo "✗ PreToolUse 注入未禁用：graphify/__main__.py 中 _SETTINGS_HOOK append 未被注释"
    echo "  应在 _install_claude_hook 函数中注释掉 PreToolUse 注入逻辑（与 context-mode 冲突）"
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
        "SessionStart:start-graphify-server.sh:start-graphify-server.sh"
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
