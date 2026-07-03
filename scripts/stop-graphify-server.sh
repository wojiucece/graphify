#!/usr/bin/env bash
# SessionEnd hook：后台更新 graph.json（不阻塞 Claude Code 退出）
# 设计：学习 mempalace 的 "background the hook and return immediately" 策略。
#       update 用 nohup 后台跑，stop 脚本立即返回（<1s），Claude Code 立即退出。
#       update 在后台完成 graph.json 更新，下次会话加载最新图谱。
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
# nohup + disown：update 脱离 hook 进程树，Claude Code 退出后继续跑完
if [ -n "$cwd" ] && [ -d "$cwd/graphify-out" ]; then
    cd "$cwd" 2>/dev/null && nohup graphify update . > /tmp/graphify-update.log 2>&1 &
    disown
fi

# 不 kill server（多会话共享，server 常驻）
# 手动清理用 scripts/kill-graphify-server.sh

