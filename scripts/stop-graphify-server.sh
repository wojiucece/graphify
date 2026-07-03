# SessionEnd hook：后台更新 graph.json（不阻塞 Claude Code 退出，带项目级文件锁防并发）
# 设计：学习 mempalace 的 "background the hook and return immediately" 策略。
#       update 用 nohup 后台跑，stop 脚本立即返回（<1s），Claude Code 立即退出。
#       项目级文件锁（mkdir 原子操作）防止多个窗口同时退出时并发 update 同一 graph.json。
#       不 kill server（多会话共享，手动清理用 kill-graphify-server.sh）。
#
# === 版本标记 ===
# 基于 graphify v0.9.5
# 依赖 CLI: graphify update .
# 依赖路径: graphify-out/
# 上游变动检查: 若 graphify 改 update 命令名，需更新此脚本

input=$(cat)
cwd=$(echo "$input" | python -c "import json,sys; print(json.load(sys.stdin).get('cwd',''))" 2>/dev/null)

# 后台更新 graph.json（仅当当前目录是 graphify 项目时）
if [ -n "$cwd" ] && [ -d "$cwd/graphify-out" ]; then
    # 项目级文件锁（mkdir 原子操作，防多窗口并发 update 同一 graph.json）
    LOCK_DIR="/tmp/graphify-update-$(echo "$cwd" | tr '/\\:' '___').lock"
    if mkdir "$LOCK_DIR" 2>/dev/null; then
        # 获得锁：后台跑 update，完成后删锁
        cd "$cwd" 2>/dev/null
        (graphify update . > /tmp/graphify-update.log 2>&1; rmdir "$LOCK_DIR" 2>/dev/null) &
        disown
    fi
    # 锁已存在 → 已有 update 在跑，跳过（幂等）
fi

# 不 kill server（多会话共享，server 常驻）
# 手动清理用 scripts/kill-graphify-server.sh
