# SessionStart hook：后台启动 graphify HTTP server
# 幂等：无 graph.json 或 server 已在跑时跳过
# 设计：hook 立即返回（不等 server 启动），server 在后台冷启动
#       首次 prompt-hook 查询时若 server 未就绪，会 fallback 到本地查询
#
# === 版本标记 ===
# 基于 graphify v0.9.5
# 依赖 CLI: graphify serve --transport http --port 8765
# 依赖端点: /health (CUSTOM, serve.py _build_server 闭包)
# 依赖路径: graphify-out/graph.json
# 上游变动检查: 若 graphify 改 CLI 命令名或 /health 端点消失，需更新此脚本

input=$(cat)
cwd=$(echo "$input" | python -c "import json,sys; print(json.load(sys.stdin).get('cwd',''))" 2>/dev/null)
[ -z "$cwd" ] && exit 0
cd "$cwd" || exit 0
[ ! -f "graphify-out/graph.json" ] && exit 0          # 无 graph.json 则跳过
curl -s http://127.0.0.1:8765/health > /dev/null 2>&1 && exit 0  # 已在跑则跳过

# v3 修订：用 nohup + disown 创建独立进程（不随 hook 退出被清理）
# 注意：$! 是 nohup 的 PID，不是 graphify 的真实 PID
# stop 脚本用 netstat 找 8765 端口的监听进程，不依赖此 PID 文件
nohup graphify serve --transport http --port 8765 > /tmp/graphify-serve.log 2>&1 &
disown
echo $! > /tmp/graphify-serve.pid
