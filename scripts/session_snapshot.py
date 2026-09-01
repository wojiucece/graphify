"""PreCompact 会话快照（A4，v1.13 零等待）：hook detach 触发 rebuild 后立即读当前磁盘
graph.json 实时计算 god nodes + 社区标题二元组，写 .session-snapshot.json（主机制）.

watch/SessionEnd 高频触发保证图通常接近新鲜；恰处 rebuilding 窗口则内容标 degraded；
rebuild 为下一次会话服务。显式排除 diff 摘要（C3 职责）与查询历史."""
from __future__ import annotations
import json, sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

def _load_graph_json(graph_json: Path):
    """R5-4：detach rebuild 落盘与快照读取的撕裂读竞态（write_text 非原子，毫秒级窗口）
    ——ValueError 后 sleep 1s 重试一次，仍失败才放弃（返回 None -> 快照空，SessionStart 侧
    sys.exit(0) 合法降级）."""
    import time
    for delay in (0, 1.0):
        if delay:
            time.sleep(delay)
        try:
            return json.loads(Path(graph_json).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return None

def snapshot_text(graph_json: Path, top_n: int = 5, state_path: Path | None = None) -> str:
    data = _load_graph_json(graph_json)
    if data is None:
        return ""
    nodes = data.get("nodes", [])
    if not nodes:
        return ""
    import networkx as nx
    G = nx.Graph() if not data.get("directed") else nx.DiGraph()
    for n in nodes:
        G.add_node(n.get("id"), **{k: n[k] for k in ("label", "community", "community_name") if k in n})
    for e in data.get("links") or data.get("edges", []):
        if G.has_node(e["source"]) and G.has_node(e["target"]):
            G.add_edge(e["source"], e["target"])
    gods = sorted(G.nodes, key=lambda n: G.degree(n), reverse=True)[:top_n]
    lines = ["[会话快照] 当前项目图核心事实：", "核心节点（度数 top）:"]
    for nid in gods:
        d = G.nodes[nid]
        lines.append(f"  - {d.get('label', nid)} (degree={G.degree(nid)})")
    names = {}
    for n in nodes:
        if n.get("community_name") and n.get("community") is not None:
            names.setdefault(n["community"], n["community_name"])
    lines.append("主要社区:")
    for cid, name in list(names.items())[:top_n]:
        lines.append(f"  - 社区{cid}: {name}")
    if state_path is not None:   # Q-1 零等待：重建窗口内诚实标注
        try:
            if json.loads(Path(state_path).read_text(encoding="utf-8")).get("phase") == "rebuilding":
                lines.append("（注：图重建中，以上为当前快照，内容标 degraded）")
        except (OSError, ValueError):
            pass
    return "\n".join(lines)

if __name__ == "__main__":
    # argv[1]=project root；快照写 <root>/graphify-out/.session-snapshot.json（主机制落盘）
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
    out_dir = root / "graphify-out"
    text = snapshot_text(out_dir / "graph.json",
                         state_path=out_dir / ".rebuild-state.json")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / ".session-snapshot.json").write_text(
        json.dumps({"text": text, "generated_at": __import__("time").time()},
                   ensure_ascii=False), encoding="utf-8")
    print(text or "（图不可用，快照为空）")
    sys.exit(0)   # 压缩流程永不被快照阻塞（A4 验收第 2 条）
