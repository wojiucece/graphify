set -euo pipefail

# ==============================================
# Graphify 自定义版本同步脚本（v3: Windows Git Bash 适配）
# 用法: bash scripts/sync.sh
# ==============================================

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
cd "$REPO_DIR"

if ! git rev-parse --git-dir > /dev/null 2>&1; then
    log_error "不在 git 仓库中"; exit 1
fi
if ! git remote get-url upstream > /dev/null 2>&1; then
    log_error "upstream 未配置，请运行:"
    echo "  git remote add upstream https://github.com/safishamsi/graphify.git"
    exit 1
fi

CURRENT_BRANCH=$(git branch --show-current)
if [[ "$CURRENT_BRANCH" != "v8-custom" ]]; then
    log_warn "当前分支是 $CURRENT_BRANCH，建议在 v8-custom 分支操作"
    read -p "是否继续？[y/N] " -n 1 -r; echo
    [[ ! $REPLY =~ ^[Yy]$ ]] && exit 0
fi

if ! git diff --quiet || ! git diff --cached --quiet; then
    log_warn "工作区有未提交的改动，请先 commit 或 stash"
    git status --short; exit 1
fi

log_info "拉取上游最新代码..."
git fetch upstream

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse upstream/v8)
if [[ "$LOCAL" == "$REMOTE" ]]; then
    log_info "已经是最新版本"; exit 0
fi

NEW_COMMITS=$(git rev-list --count HEAD..upstream/v8)
log_info "上游有 $NEW_COMMITS 个新提交"
echo ""
log_info "上游最近更新:"
git log --oneline HEAD..upstream/v8 | head -10
echo ""
read -p "是否开始同步（rebase）？[y/N] " -n 1 -r; echo
[[ ! $REPLY =~ ^[Yy]$ ]] && exit 0

# 备份 tag
git tag "backup/before-sync-$(date +%Y%m%d)" 2>/dev/null || true

log_info "开始变基到 upstream/v8..."
if git rebase upstream/v8; then
    log_info "Rebase 成功，无冲突！"
else
    log_error "Rebase 遇到冲突，请手动解决:"
    echo ""
    echo "冲突文件:"
    git diff --name-only --diff-filter=U
    echo ""
    echo "解决步骤:"
    echo "  1. 打开冲突文件，搜索 <<<<<<< 找到冲突位置"
    echo "  2. 保留上游新代码，重新插入 CUSTOM: 标记的你的改动"
    echo "  3. git add <文件>"
    echo "  4. git rebase --continue"
    echo "  5. 放弃: git rebase --abort"
    echo ""
    echo "快速搜索你的改动: git grep CUSTOM:"
    echo ""
    # v3 修订（审核优化 #1）：冲突时打开交互式 shell，让用户现场解决
    $SHELL
    if git rebase --show-current-patch > /dev/null 2>&1; then
        log_warn "Rebase 还在进行中"; exit 1
    fi
fi

log_info "验证安装..."
if command -v graphify > /dev/null 2>&1; then
    log_info "graphify: $(graphify --version 2>&1 || echo unknown)"
else
    log_warn "graphify 未找到"
fi

# v3 新增：验证 CUSTOM 改动完整
log_info "验证 CUSTOM 改动完整..."
bash scripts/check-custom.sh

# v3 新增：prompt-hook 冒烟测试
log_info "prompt-hook 冒烟测试..."
if echo '{"prompt":"test","cwd":"/tmp"}' | graphify prompt-hook 2>/dev/null; then
    log_info "prompt-hook 可用"
else
    log_warn "prompt-hook 冒烟测试失败（可能需要重新 graphify claude install）"
fi

log_info "推送到 fork..."
git push origin v8-custom --force-with-lease

log_info "刷新 editable 安装..."
uv tool install --editable ".[mcp,openai]" --force 2>/dev/null || {
    log_warn "刷新失败，请手动运行: uv tool install --editable \".[mcp,openai]\" --force"
}

# v4 新增：验证全局 hook 配置（SessionStart/SessionEnd/PreCompact 是手动配置的，sync 不覆盖）
log_info "验证全局 hook 配置完整性..."
SETTINGS="$HOME/.claude/settings.json"
if [ -f "$SETTINGS" ]; then
    # 检查 SessionEnd 是否包含 sessionend-graphify-update.sh
    if grep -q "sessionend-graphify-update.sh" "$SETTINGS" 2>/dev/null; then
        log_info "SessionEnd → sessionend-graphify-update.sh 已注册"
    else
        log_warn "SessionEnd → sessionend-graphify-update.sh 未注册"
        log_warn "请手动添加: D:/code/graphify_fork/scripts/sessionend-graphify-update.sh 到 ~/.claude/settings.json 的 SessionEnd hook"
    fi
    # 检查 SessionStart 是否包含 sessionstart-graphify-server.sh
    if grep -q "sessionstart-graphify-server.sh" "$SETTINGS" 2>/dev/null; then
        log_info "SessionStart → sessionstart-graphify-server.sh 已注册"
    else
        log_warn "SessionStart → sessionstart-graphify-server.sh 未注册"
    fi
    # 检查 PreCompact 是否包含 precompact-graphify-update.sh
    if grep -q "precompact-graphify-update.sh" "$SETTINGS" 2>/dev/null; then
        log_info "PreCompact → precompact-graphify-update.sh 已注册"
    else
        log_warn "PreCompact → precompact-graphify-update.sh 未注册"
    fi
else
    log_warn "~/.claude/settings.json 不存在，hook 配置未注册"
fi

echo ""
log_info "========================================="
log_info "同步成功！"
log_info "回退命令: git reset --hard backup/before-sync-$(date +%Y%m%d)"
log_info "========================================="
