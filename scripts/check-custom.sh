set -euo pipefail
FAILS=0
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
# 分层融合自定义面（Task 13-16，feat/codegraph-merge）：scripts/ 下四个自定义脚本与上游 graphify/ 零交叠
# adapter.py=codegraph DB 只读适配器；run_analysis.py=编排器；split_semantic_seed.py=旧图 semantic 种子拆分；rebuild_entry.py=单一重建入口
# Phase 4 新增测试（Task 1-14）与 benchmarks/ 基准脚本——上游 merge 丢弃这些文件时守护必须告警
# native-indexing Task 04（resolved_by 打点 + 失败收集器）：测试与 fixture——上游 merge 丢弃时守护必须告警
# native-indexing Task 01（提取契约 Python 五件套）：tracer-bullet 测试与 fixture——上游 merge 丢弃时守护必须告警
# native-indexing Task 02（提取契约 15 语言扩展 + 变量签名三条规则）：全语言契约测试——上游 merge 丢弃时守护必须告警
for f in graphify/prompt_hook.py scripts/sync.sh scripts/sessionstart-graphify-server.sh scripts/precompact-graphify-update.sh scripts/sessionend-graphify-update.sh scripts/check-custom.sh scripts/adapter.py scripts/run_analysis.py scripts/split_semantic_seed.py scripts/rebuild_entry.py benchmarks/efficiency_benchmark.py tests/test_rebuild_state.py tests/test_response_envelope.py tests/test_redaction.py tests/test_adapter_snapshot.py tests/test_session_snapshot.py tests/test_cache_gc.py tests/test_ranked_context.py tests/test_symbol_source.py tests/test_dispatch_trace.py tests/test_git_symbols.py tests/test_hotspots.py tests/test_structure_queries.py tests/test_schema_budget.py tests/test_efficiency_benchmark.py tests/test_resolved_by_and_gap_collector.py tests/fixtures/resolved_by/python/pkg/__init__.py tests/fixtures/resolved_by/python/pkg/callee.py tests/fixtures/resolved_by/python/pkg/caller.py tests/fixtures/resolved_by/python/pkg/orphan.py tests/fixtures/resolved_by/python/pkg/class_def.py tests/fixtures/resolved_by/python/pkg/class_use.py tests/fixtures/resolved_by/typescript/repo.ts tests/fixtures/resolved_by/typescript/use.ts tests/test_extraction_contract.py tests/fixtures/sample_native_fields.py tests/test_extraction_contract_languages.py; do
    if [ -f "$f" ]; then
        echo "✓ $f"
    else
        echo "✗ $f 缺失"
        FAILS=$((FAILS+1))
    fi
done

echo ""
echo "=== PreToolUse 注入禁用检查（避免与 context-mode 的 Read/Bash hook 冲突）==="
# _install_claude_hook 中 PreToolUse 注入应被注释掉，只保留 UserPromptSubmit
# 上游 0.9.8 重构：PreToolUse 注入从 append(_SETTINGS_HOOK) 改为 extend(_claude_pretooluse_hooks())
# 上游 0.9.20 给 _claude_pretooluse_hooks() 加了 strict 参数（默认 False），grep 模式不锁死参数
# 恢复方式：取消 _install_claude_hook 函数中被注释的 extend(_claude_pretooluse_hooks(...)) 行
if grep -q '^    # hooks\["PreToolUse"\].extend(_claude_pretooluse_hooks' graphify/install.py 2>/dev/null; then
    echo "✓ PreToolUse 注入已禁用（_install_claude_hook 中 _claude_pretooluse_hooks(...) extend 被注释）"
else
    echo "✗ PreToolUse 注入未禁用：graphify/install.py 中 _claude_pretooluse_hooks(...) extend 未被注释"
    echo "  应在 _install_claude_hook 函数中注释掉 hooks[\"PreToolUse\"].extend(_claude_pretooluse_hooks(...)) 行（与 context-mode 冲突）"
    FAILS=$((FAILS+1))
fi

echo ""
echo "=== UserPromptSubmit 注入检查（prompt-hook 是 fork 核心，必须启用）==="
# _install_claude_hook 中 UserPromptSubmit 注入必须存在且未注释
# rebase 时若这段被上游覆盖，prompt-hook 失效但其他检查全绿--隐性丢功能
if grep -q '^    hooks\["UserPromptSubmit"\].append(_PROMPT_HOOK)' graphify/install.py 2>/dev/null; then
    echo "✓ UserPromptSubmit 注入存在（_PROMPT_HOOK append 未注释，prompt-hook 生效）"
else
    echo "✗ UserPromptSubmit 注入缺失：graphify/install.py 中 _PROMPT_HOOK append 未找到或被注释"
    echo "  应在 _install_claude_hook 函数中保留 hooks[\"UserPromptSubmit\"].append(_PROMPT_HOOK)（CUSTOM: add UserPromptSubmit hook 段）"
    FAILS=$((FAILS+1))
fi

echo ""
echo "=== Fork 版本号检查（区分本地 fork 与上游 graphifyy）==="
# pyproject.toml 中 version 应带 +fork 后缀，区分本地构建与上游发布版
# _version_tuple 解析 "0.9.5+fork.1" -> (0,9,5,1) > 上游 (0,9,5)，版本比较正确
FORK_VER=$(grep '^version = ' pyproject.toml | head -1 | sed 's/^version = "\([^"]*\)".*/\1/')
case "$FORK_VER" in
    *+fork*)
        echo "✓ Fork 版本号标识存在: $FORK_VER"
        ;;
    *)
        echo "✗ Fork 版本号标识缺失：pyproject.toml 中 version 应带 +fork 后缀（当前: $FORK_VER）"
        echo "  例: version = \"0.9.5+fork.1\""
        FAILS=$((FAILS+1))
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
            echo "✓ $f -> $ver"
            ;;
        *)
            echo "✗ $f -> $ver（上游版！无 prompt-hook 命令）"
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
            FAILS=$((FAILS+1))
            ;;
    esac
done < <(find /d -maxdepth 6 -name "graphify.exe" 2>/dev/null)
if [ "$GRAPHIFY_FOUND" = "0" ]; then
    echo "✗ 未找到任何 graphify.exe（可能未安装）"
    FAILS=$((FAILS+1))
fi

# 全局命令版本（PATH 解析到的）
global_ver=$(graphify --version 2>&1 | head -1)
case "$global_ver" in
    *+fork*) echo "✓ 全局 graphify（PATH 解析）-> $global_ver" ;;
    *)
        echo "✗ 全局 graphify（PATH 解析）-> $global_ver（上游版！重装: uv tool install --editable D:/code/graphify_fork --force）"
        FAILS=$((FAILS+1))
        ;;
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
            echo "✓ $event -> $script_name 已注册"
        else
            echo "✗ $event -> $script_name 未注册（需手动添加到 ~/.claude/settings.json 的 $event hook）"
            all_ok=1
            FAILS=$((FAILS+1))
        fi
    done
    [ "$all_ok" = "0" ] && echo "  所有生命周期 hook 完整" || true
else
    echo "✗ ~/.claude/settings.json 不存在"
    FAILS=$((FAILS+1))
fi

echo ""
echo "=== OpenCode hook 检查（_OPENCODE_PLUGIN_JS 的 before + after）==="
# OpenCode 走 _OPENCODE_PLUGIN_JS（install.py）注册 tool.execute.before/after
# before：bash 拼 echo（执行前提醒）；after：read/write/edit/glob/grep 改 output.output（执行后提醒）
# after 是方案 C 新增，rebase 时若被上游覆盖，OpenCode 非 bash 工具不提醒--隐性丢功能
MAIN_PY="graphify/install.py"
if grep -q '"tool.execute.before"' "$MAIN_PY" 2>/dev/null; then
    echo "✓ OpenCode before hook 存在（bash 拼 echo）"
else
    echo "✗ OpenCode before hook 缺失：$MAIN_PY 中 tool.execute.before 未找到"
    FAILS=$((FAILS+1))
fi
if grep -q '"tool.execute.after"' "$MAIN_PY" 2>/dev/null; then
    echo "✓ OpenCode after hook 存在（read/write/edit/glob/grep 改 output.output）"
else
    echo "✗ OpenCode after hook 缺失：$MAIN_PY 中 tool.execute.after 未找到（方案 C 丢失，非 bash 工具不提醒）"
    FAILS=$((FAILS+1))
fi
# after 引导句语气检查：用 run（指令）非 try（建议）
if grep -q 'next time run graphify' "$MAIN_PY" 2>/dev/null; then
    echo "✓ after 引导句用 run（指令语气）"
elif grep -q 'next time try graphify' "$MAIN_PY" 2>/dev/null; then
    echo "✗ after 引导句用 try（建议语气偏软，应改 run）"
    FAILS=$((FAILS+1))
fi
# after fail-open guard 检查
if grep -q 'typeof output.output !== "string"' "$MAIN_PY" 2>/dev/null; then
    echo "✓ after fail-open guard 存在（output.output 非 string 时跳过，防崩）"
else
    echo "✗ after fail-open guard 缺失（output.output 非 string 时会崩）"
    FAILS=$((FAILS+1))
fi

echo ""
echo "=== 汇总 ==="
if [ "$FAILS" = "0" ]; then
    echo "✓ 所有 fork 定制检查通过"
    exit 0
else
    echo "✗ $FAILS 项检查失败（见上方 ✗ 标记）"
    exit "$FAILS"
fi
