#!/usr/bin/env bash
# SessionStart hook (matcher: compact)：读 PreCompact 落盘的会话快照 -> stdout 注入.
# 官方确定通道（v1.13 R17）：SessionStart stdout 注入上下文；快照缺失/为空则静默退出。
# 路径联动总条款（E2 修复）：cwd 从 hook stdin 的 JSON 取（与 precompact 脚本同模式），
# 不写死任何客户端目录惯例。
# 注册：~/.claude/settings.json SessionStart hooks 手动添加（沿 CLAUDE.md 惯例，
# graphify claude install 不注册生命周期 hooks）。形态参照既有
# sessionstart-graphify-server.sh 的条目（{"command": ..., "timeout": 10, "type": "command"}），
# matcher 取 "compact"（对应压缩后新会话；若本机版本 matcher 命名不同，沿用既有条目的
# 无 matcher 形态亦可——脚本对非 compact 时机触发同样安全，注入的是最近一次 PreCompact
# 落盘的静态快照）。
# PYTHONUTF8=1：Windows 下 python stdout 默认 GBK，Claude Code 按 UTF-8 解码 hook stdout，
# 不设则中文快照注入成乱码（实测发现；与 precompact 侧快照调用的 PYTHONUTF8=1 对称）。
export PYTHONUTF8=1
cwd=$(python -c "import json,sys;print(json.load(sys.stdin).get('cwd',''))" 2>/dev/null)
[ -z "$cwd" ] && exit 0
SNAP="$cwd/graphify-out/.session-snapshot.json"
python - "$SNAP" <<'EOF'
import json, sys
from pathlib import Path
try:
    d = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    text = d.get("text", "")
except (OSError, ValueError):
    sys.exit(0)
if text:
    print(text)   # SessionStart stdout = 注入上下文（官方通道）
sys.exit(0)
EOF
