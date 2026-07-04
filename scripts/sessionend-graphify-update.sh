# SessionEnd hook：在会话结束前更新 graph.json
# 与 precompact-graphify-update.sh 逻辑一致，但使用 detached 模式（不阻塞会话退出）
# 场景：/Compact 会压缩上下文，压缩后重建时用最新 graph.json
# 并发保护：项目级文件锁（mkdir 原子操作），防多窗口同时 SessionEnd 时并发 update
#
# === 版本标记 ===
# 基于 graphify v0.9.5+fork.1
# 依赖 CLI: graphify update .
# 依赖路径: graphify-out/（目录存在才执行 update）
# 上游变动检查: 若 graphify 改 update 命令名，需更新此脚本

input=$(cat)
cwd=$(echo "$input" | python -c "import json,sys; print(json.load(sys.stdin).get('cwd',''))" 2>/dev/null)
[ -z "$cwd" ] && exit 0
cd "$cwd" || exit 0
[ ! -d "graphify-out" ] && exit 0          # 不是 graphify 项目则跳过

# 项目级文件锁（mkdir 原子操作，防多窗口并发 update 同一 graph.json）
LOCK_DIR="/tmp/graphify-update-$(echo "$cwd" | tr '/\\:' '___').lock"
if mkdir "$LOCK_DIR" 2>/dev/null; then
    # 获得锁：后台执行 update（不阻塞 SessionEnd），完成后删锁
    graphify update . > /tmp/graphify-sessionend-update.log 2>&1
    rmdir "$LOCK_DIR" 2>/dev/null
fi
# 锁已存在 → 已有 update 在跑，跳过（幂等）