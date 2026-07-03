#!/usr/bin/env bash
# SessionStart hook：后台启动 graphify HTTP server
# 幂等：无 graph.json 或 server 已在跑时跳过
# 设计：hook 立即返回（不等 server 启动），server 在后台冷启动
#       首次 prompt-hook 查询时若 server 未就绪，会 fallback 到本地查询

input=$(cat)
cwd=$(echo "$input" | python -c "import json,sys; print(json.load(sys.stdin).get('cwd',''))" 2>/dev/null)
[ -z "$cwd" ] && exit 0
cd "$cwd" || exit 0
[ ! -f "graphify-out/graph.json" ] && exit 0          # 无 graph.json 则跳过
curl -s http://127.0.0.1:8765/health > /dev/null 2>&1 && exit 0  # 已在跑则跳过

graphify serve --transport http --port 8765 > /tmp/graphify-serve.log 2>&1 &
echo $! > /tmp/graphify-serve.pid
