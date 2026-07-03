#!/usr/bin/env bash
# SessionEnd hook：更新 graph.json（不 kill server，多会话共享）
# 设计：只 update，不 kill server。server 常驻，多会话共享。
#       手动 kill 用 scripts/kill-graphify-server.sh，或系统重启自动清理。
#       update 是纯 AST 提取（no LLM），确保下次会话用最新图谱。
#
# === 版本标记 ===
# 基于 graphify v0.9.5
# 依赖 CLI: graphify update .
# 依赖路径: graphify-out/
# 上游变动检查: 若 graphify 改 update 命令名，需更新此脚本

input=$(cat)
cwd=$(echo "$input" | python -c "import json,sys; print(json.load(sys.stdin).get('cwd',''))" 2>/dev/null)

# 更新 graph.json（仅当当前目录是 graphify 项目时）
if [ -n "$cwd" ] && [ -d "$cwd/graphify-out" ]; then
    cd "$cwd" 2>/dev/null && graphify update . > /tmp/graphify-update.log 2>&1
fi

# 不 kill server（多会话共享，server 常驻）
# 手动清理用 scripts/kill-graphify-server.sh
