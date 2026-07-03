# 手动停止 graphify HTTP server（多会话场景下手动清理用）
# 正常会话结束用 stop-graphify-server.sh（只 update 不 kill）
# 此脚本用于：系统维护、端口冲突、server 异常、关机前手动清理
#
# === 版本标记 ===
# 基于 graphify v0.9.5
# 依赖：netstat + taskkill（Windows）
# 端口：8765（GRAPHIFY_MCP_PORT 默认值）

PID=$(netstat -ano 2>/dev/null | grep ":8765" | grep LISTENING | awk '{print $NF}' | head -1)
if [ -n "$PID" ]; then
    taskkill //PID $PID //T //F 2>/dev/null
    echo "[OK] killed graphify server (PID $PID)"
    rm -f /tmp/graphify-serve.pid
else
    echo "[INFO] graphify server not running on port 8765"
fi
