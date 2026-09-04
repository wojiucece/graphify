# PreCompact hook：在 /Compact 前更新 graph.json
# 与 mempalace 的 PreCompact hook 同时机执行
# 设计：仅触发 rebuild_entry（后台 detach，不阻塞 /Compact），server 检测到 graph.json 变化会热重载
# 场景：/Compact 会压缩上下文，压缩后重建时用最新 graph.json
# 并发保护：rebuild_entry.py 内部 mkdir 原子锁（跨三触发面互斥，含 stale 接管）
#
# === 版本标记 ===
# 基于 graphify v0.9.5
# 依赖: scripts/rebuild_entry.py（单一重建入口：extract→build→to_json→rebuild_fts 新链路 + 锁/指纹收敛；Task 09 codegraph 退役）
# 依赖路径: graphify-out/（目录存在才执行 rebuild）
# 上游变动检查: 若 rebuild_entry 改名，需更新此脚本

input=$(cat)
cwd=$(echo "$input" | python -c "import json,sys; print(json.load(sys.stdin).get('cwd',''))" 2>/dev/null)
[ -z "$cwd" ] && exit 0
cd "$cwd" || exit 0
[ ! -d "graphify-out" ] && exit 0          # 不是 graphify 项目则跳过

# 项目级文件锁（mkdir 原子操作，防多窗口并发触发同一 rebuild）
LOCK_DIR="/tmp/graphify-update-$(echo "$cwd" | tr '/\\:' '___').lock"
if mkdir "$LOCK_DIR" 2>/dev/null; then
    # E 裁决：旧脚本注释自称 detached 但实现本就同步（graphify update . > log 无 &）。
    # rebuild_entry 是分钟级（extract + 全量 build/cluster/analyze/report），hook 面同步执行会超时强杀
    # -> finally 不执行 -> 锁残留 -> 后续三触发面全 exit 3。改为后台 detach（与注释意图一致），
    # rebuild_entry 内的 stale 锁检测兜底强杀场景。
    # Task 11 收尾：codegraph 运行时退役，移除旧 codegraph 目录判别门控——所有项目
    # 统一路由 rebuild_entry（新链路单一重建入口 extract→build→to_json→rebuild_fts→分析）。
    SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
    PYTHON="${GRAPHIFY_PYTHON:-python}"
    nohup "$PYTHON" "$SCRIPT_DIR/rebuild_entry.py" --project "$cwd" > /tmp/graphify-rebuild.log 2>&1 &
    # A4 (v1.13): 零等待——直接读当前磁盘 graph.json 实时算快照落盘（主机制）；
    # SessionStart(matcher:compact) hook 负责注入，本脚本只落盘不注入。
    # 不等待 rebuild（timeout 实配 30s，同步等待分钟级 rebuild 必超时强杀致锁残留，见 E 裁决注释）
    PYTHONUTF8=1 python "$SCRIPT_DIR/session_snapshot.py" "$cwd" > /tmp/graphify-snapshot.log 2>&1
    rmdir "$LOCK_DIR" 2>/dev/null
fi
# 锁已存在 → 已有触发在跑，跳过（幂等；真正的互斥由 rebuild_entry 内部锁承担）
