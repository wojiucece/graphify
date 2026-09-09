#!/bin/bash
# 确保 graphify HTTP server 存活（R3 自愈闭环 + SessionStart 共用单一事实源）。
# 幂等：无 graph.json 或 server 已在跑时跳过。
#
# 用法: ensure-graphify-server.sh [cwd]
#   - cwd 缺省时从 stdin JSON（Claude Code hook 格式）读 {"cwd": ...}
#   - 依赖 CLI: graphify-mcp --transport http --port <port> --watch
#     （serve 入口是 pyproject [project.scripts]；`graphify serve` 命令不存在）
#   - 依赖端点: /health (CUSTOM, serve.py _build_server 闭包)
#   - 依赖路径: graphify-out/graph.json
#
# 防抖（拉起风暴防护，跨进程持久化——prompt_hook 每 prompt 新进程，进程内状态不可靠）：
# 先探活（/health 通了直接退出）→ 失败后的重复拉起由 launch-marker 条款抑制
# （marker <30s 新鲜则跳过拉起）。覆盖两个竞态：server 启动中端口未开（2-4s 窗口内
# 重复 prompt 重复拉起）与双 prompt 并发复活。curl 加 --max-time 上限（防挂死
# server 卡 prompt）。
#
# 端口单一事实源：prompt_hook 连 GRAPHIFY_MCP_PORT（默认 8765）——ensure 拉起必须落在
# 同一端口，否则自愈环断裂（prompt 查 8765 / server 起 9000）。GRAPHIFY_SERVE_PORT 已删除
# （I2 final review：注释说废弃却保留生效路径，两份报告同判；删回退防再次分裂）。
#
# 复活 default 漂移裁决（spec serve-memory §R3）：拉起者 cwd 成为 pinned default——
# prompt-hook 恒传 project_root，default 几乎无消费方，接受漂移。
#
# === 版本标记 ===
# 基于 graphify v0.9.5；R3（serve-memory）从 sessionstart-graphify-server.sh 抽取
# health-check + nohup 启动为单一事实源（启动行为不变），prompt_hook 自愈复用同一脚本。
# 上游变动检查: 若 graphify 改 CLI 命令名或 /health 端点消失，需更新此脚本。

PORT="${GRAPHIFY_MCP_PORT:-8765}"
MARKER="${GRAPHIFY_SERVE_LAUNCH_MARKER:-/tmp/graphify-serve.launch}"

cwd="${1:-}"
if [ -z "$cwd" ]; then
    input=$(cat)
    cwd=$(echo "$input" | python -c "import json,sys; print(json.load(sys.stdin).get('cwd',''))" 2>/dev/null)
fi
[ -z "$cwd" ] && exit 0
cd "$cwd" || exit 0
[ ! -f "graphify-out/graph.json" ] && exit 0          # 无 graph.json 则跳过

# launch-marker 防抖：<30s 内拉过则跳过（5s 级短路——真死场景省每条 prompt 的 2s 探活；
# 行为等价：marker 新鲜时旧序也因 marker 检查退出，仅省一次探活开销）
now=$(date +%s)
if [ -f "$MARKER" ]; then
    last=$(cat "$MARKER" 2>/dev/null || echo 0)
    if [ $((now - last)) -lt 30 ]; then
        exit 0
    fi
fi
# 探活：通了直接退出（不重复拉起，幂等；marker 过期 + 存活 → 探活退出，不覆写 marker）
curl -s --max-time 2 "http://127.0.0.1:${PORT}/health" > /dev/null 2>&1 && exit 0
echo "$now" > "$MARKER"

# v3 修订：用 nohup + disown 创建独立进程（不随 hook 退出被清理）
# 注意：$! 是 nohup 的 PID，不是 graphify 的真实 PID
# stop 脚本用 netstat 找端口监听进程，不依赖此 PID 文件
nohup graphify-mcp --transport http --port "$PORT" --watch > /tmp/graphify-serve.log 2>&1 &
disown
echo $! > /tmp/graphify-serve.pid
