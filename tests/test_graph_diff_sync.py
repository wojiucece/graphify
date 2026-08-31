"""graph_diff 接 codegraph sync：上轮产物 vs 本次重建（B1 单库形态）."""
import json
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"
MINI_DB = FIXTURES / "codegraph-fixtures" / "mini.codegraph.db"


def test_diff_prev_vs_current(tmp_path):
    from run_analysis import run, diff
    # 先跑一次产出 prev_graph
    out1 = run(MINI_DB, output_dir=tmp_path / "v1", root="fixture-src")
    prev = out1 / "graph.json"
    # 同 DB 重建 -> diff 应为空（无变更）
    result = diff(prev, MINI_DB, root="fixture-src")
    # graph_diff 真实返回键名（analyze.py:556 核读）：new_nodes/removed_nodes/new_edges/removed_edges/summary
    assert "new_nodes" in result and "removed_nodes" in result
    assert result.get("new_nodes", []) == []
    assert result.get("removed_nodes", []) == []


def test_diff_detects_changes(tmp_path):
    """prev_graph 缺一个节点 vs 本次完整 -> 检出 new_nodes."""
    from run_analysis import run, diff
    out = run(MINI_DB, output_dir=tmp_path / "full", root="fixture-src")
    full = json.loads((out / "graph.json").read_text(encoding="utf-8"))
    # 造一个缺节点的 prev（node_link_graph 会经 links 重建端点节点——只删 nodes 不删
    # 关联 links 时被删节点会复活导致 new_nodes 恒空；须连 incident links 一起删）
    removed = full["nodes"][-1]["id"]
    full["nodes"] = full["nodes"][:-1]
    full["links"] = [l for l in full["links"] if l["source"] != removed and l["target"] != removed]
    prev = tmp_path / "prev.json"
    prev.write_text(json.dumps(full), encoding="utf-8")
    # root 必须与 run 一致，否则 build_from_json 的 _norm_source_file 归一化不同 -> 假 diff
    result = diff(prev, MINI_DB, root="fixture-src")
    assert len(result.get("new_nodes", [])) >= 1
