#!/bin/bash
# SessionStart hook：后台启动 graphify HTTP server
# 幂等：无 graph.json 或 server 已在跑时跳过
# 设计：hook 立即返回（不等 server 启动），server 在后台冷启动
#       首次 prompt-hook 查询时若 server 未就绪，会 fallback 到本地查询
#
# R3（serve-memory）：health-check + nohup 启动抽取至 ensure-graphify-server.sh
# 单一事实源（启动行为不变）——本脚本只把 hook stdin（JSON，含 cwd）转发给共享脚本，
# prompt_hook 自愈分支复用同一脚本（server 死后非阻塞拉起，防抖由脚本 launch-marker 背书）。
#
# === 版本标记 ===
# 基于 graphify v0.9.5
# 依赖 CLI: graphify-mcp --transport http --port 8765 --watch（serve 入口是 pyproject [project.scripts]
#           graphify-mcp = graphify.serve:_main；`graphify serve` 命令不存在——2026-09-04 实测
#           unknown command，SessionStart 曾因此拉不起 server，已改）
# 依赖端点: /health (CUSTOM, serve.py _build_server 闭包)
# 依赖路径: graphify-out/graph.json
# 上游变动检查: 若 graphify 改 CLI 命令名或 /health 端点消失，需更新 ensure-graphify-server.sh

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec bash "$SCRIPT_DIR/ensure-graphify-server.sh"
