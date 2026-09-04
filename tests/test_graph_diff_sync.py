"""graph_diff：上轮 graph.json vs 当前 graph.json（Task 09 换源——双图对比，无 DB）."""
import json
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"
MINI_GRAPH = FIXTURES / "mini-graph.json"


def test_diff_prev_vs_current(tmp_path):
    from run_analysis import diff
    # 同图对比 -> diff 应为空（无变更）
    result = diff(MINI_GRAPH, MINI_GRAPH, root="fixture-src")
    # graph_diff 真实返回键名（analyze.py:556 核读）：new_nodes/removed_nodes/new_edges/removed_edges/summary
    assert "new_nodes" in result and "removed_nodes" in result
    assert result.get("new_nodes", []) == []
    assert result.get("removed_nodes", []) == []


def test_diff_detects_changes(tmp_path):
    """prev_graph 缺一个节点 vs 本次完整 -> 检出 new_nodes."""
    from run_analysis import diff
    full = json.loads(MINI_GRAPH.read_text(encoding="utf-8"))
    # 造一个缺节点的 prev（node_link_graph 会经 links 重建端点节点——只删 nodes 不删
    # 关联 links 时被删节点会复活导致 new_nodes 恒空；须连 incident links 一起删）
    removed = full["nodes"][-1]["id"]
    full["nodes"] = full["nodes"][:-1]
    full["links"] = [l for l in full["links"] if l["source"] != removed and l["target"] != removed]
    prev = tmp_path / "prev.json"
    prev.write_text(json.dumps(full), encoding="utf-8")
    # root 必须与产出两张图的 rebuild 一致，否则 source_file 归一化不同 -> 假 diff
    result = diff(prev, MINI_GRAPH, root="fixture-src")
    assert len(result.get("new_nodes", [])) >= 1
