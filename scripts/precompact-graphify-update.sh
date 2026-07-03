#!/usr/bin/env bash
# PreCompact hook：在 /Compact 前更新 graph.json
# 与 mempalace 的 PreCompact hook 同时机执行
# 设计：仅 update（不 kill/start server），server 检测到 graph.json 变化会热重载
# 场景：/Compact 会压缩上下文，压缩后重建时用最新 graph.json

input=$(cat)
cwd=$(echo "$input" | python -c "import json,sys; print(json.load(sys.stdin).get('cwd',''))" 2>/dev/null)
[ -z "$cwd" ] && exit 0
cd "$cwd" || exit 0
[ ! -d "graphify-out" ] && exit 0          # 不是 graphify 项目则跳过

# 更新 graph.json（纯 AST，no LLM）
graphify update . > /tmp/graphify-precompact-update.log 2>&1
