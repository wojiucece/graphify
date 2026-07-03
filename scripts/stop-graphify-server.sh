#!/usr/bin/env bash
# SessionEnd hook：先更新 graph.json，再停止 graphify HTTP server
# 顺序：graphify update → 等 update 完成 → kill server
# 设计：update 是纯 AST 提取（no LLM），速度快；update 完成后才 kill server，
#       确保 graph.json 写入完整，下次会话 SessionStart 启动 server 时加载最新图谱
#
# === 版本标记 ===
# 基于 graphify v0.9.5
# 依赖 CLI: graphify update .
# 依赖路径: graphify-out/、/tmp/graphify-serve.pid
# 依赖命令: taskkill /T /F (Windows 进程树 kill)
# 上游变动检查: 若 graphify 改 update 命令名，需更新此脚本

input=$(cat)
cwd=$(echo "$input" | python -c "import json,sys; print(json.load(sys.stdin).get('cwd',''))" 2>/dev/null)
PID_FILE="/tmp/graphify-serve.pid"

# 1. 更新 graph.json（仅当 server 在跑且当前目录是 graphify 项目时）
if [ -f "$PID_FILE" ] && [ -n "$cwd" ] && [ -d "$cwd/graphify-out" ]; then
    cd "$cwd" 2>/dev/null && graphify update . > /tmp/graphify-update.log 2>&1
fi

# 2. 停止 server（update 完成后才 kill）
# v3 修订：用 netstat 找 8765 端口的监听进程（不依赖 PID 文件，因为 nohup 的 $! 不可靠）
# 用 taskkill /T /F kill 进程树（graphify serve 会 fork uvicorn 子进程）
PID=$(netstat -ano 2>/dev/null | grep ":8765" | grep LISTENING | awk '{print $NF}' | head -1)
if [ -n "$PID" ]; then
    taskkill //PID $PID //T //F 2>/dev/null
fi
rm -f "$PID_FILE"
