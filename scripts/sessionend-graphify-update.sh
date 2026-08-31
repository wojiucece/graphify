# SessionEnd hook：在会话结束前更新 graph.json
# 与 precompact-graphify-update.sh 逻辑一致，但使用 detached 模式（不阻塞会话退出）
# 场景：/Compact 会压缩上下文，压缩后重建时用最新 graph.json
# 并发保护：rebuild_entry.py 内部 mkdir 原子锁（跨三触发面互斥，含 stale 接管）
#
# === 版本标记 ===
# 基于 graphify v0.9.5+fork.1
# 依赖: scripts/rebuild_entry.py（单一重建入口：codegraph sync + 适配器重建 + 指纹收敛）
# 依赖路径: graphify-out/（目录存在才执行 rebuild）
# 上游变动检查: 若 rebuild_entry 改名，需更新此脚本

input=$(cat)
cwd=$(echo "$input" | python -c "import json,sys; print(json.load(sys.stdin).get('cwd',''))" 2>/dev/null)
[ -z "$cwd" ] && exit 0
cd "$cwd" || exit 0
[ ! -d "graphify-out" ] && exit 0          # 不是 graphify 项目则跳过

# E 裁决：旧脚本注释自称 detached 但实现本就同步（graphify update . > log 无 &）。
# rebuild_entry 是分钟级（sync + 全量 cluster/analyze/report），hook 面同步执行会超时强杀
# -> finally 不执行 -> 锁残留 -> 后续三触发面全 exit 3。改为后台 detach（与注释意图一致），
# rebuild_entry 内的 stale 锁检测兜底强杀场景。
# Fix I2：无 .codegraph 的非 codegraph 项目回退旧 graphify update 路径（rebuild_entry 会
# FileNotFoundError 静默失效，graph.json 不再更新）——与 watch 汇聚点的门控回退对称。
if [ -d "$cwd/.codegraph" ]; then
    SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
    PYTHON="${GRAPHIFY_PYTHON:-python}"
    nohup "$PYTHON" "$SCRIPT_DIR/rebuild_entry.py" --project "$cwd" > /tmp/graphify-rebuild.log 2>&1 &
else
    graphify update . > /tmp/graphify-sessionend-update.log 2>&1
fi
