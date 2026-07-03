#!/usr/bin/env bash
# SessionEnd hook：先更新 graph.json，再停止 graphify HTTP server
# 顺序：graphify update → 等 update 完成 → kill server
# 设计：update 是纯 AST 提取（no LLM），速度快；update 完成后才 kill server，
#       确保 graph.json 写入完整，下次会话 SessionStart 启动 server 时加载最新图谱

input=$(cat)
cwd=$(echo "$input" | python -c "import json,sys; print(json.load(sys.stdin).get('cwd',''))" 2>/dev/null)
PID_FILE="/tmp/graphify-serve.pid"

# 1. 更新 graph.json（仅当 server 在跑且当前目录是 graphify 项目时）
if [ -f "$PID_FILE" ] && [ -n "$cwd" ] && [ -d "$cwd/graphify-out" ]; then
    cd "$cwd" 2>/dev/null && graphify update . > /tmp/graphify-update.log 2>&1
fi

# 2. 停止 server（update 完成后才 kill）
# 用 taskkill /T /F kill 进程树（graphify serve 会 fork uvicorn 子进程，普通 kill 只杀父进程）
if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    taskkill //PID $PID //T //F 2>/dev/null || kill $PID 2>/dev/null
    rm -f "$PID_FILE"
fi
