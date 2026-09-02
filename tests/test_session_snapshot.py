"""A4 快照：god nodes + 社区标题二元组；rebuilding 窗口标 degraded；graph.json 缺失不炸."""
import json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from session_snapshot import snapshot_text

def _graph(nodes, links):
    return {"directed": False, "multigraph": False, "graph": {}, "nodes": nodes, "links": links}

def _mini_graph_file(tmp_path):
    g = tmp_path / "graph.json"
    g.write_text(json.dumps(_graph(
        [{"id": "a", "label": "alpha", "community": 1, "community_name": "core"},
         {"id": "b", "label": "beta", "community": 1, "community_name": "core"},
         {"id": "c", "label": "gamma", "community": 2, "community_name": "io"}],
        [{"source": "a", "target": "b"}, {"source": "a", "target": "c"},
         {"source": "b", "target": "c"}, {"source": "a", "target": "b"}])), encoding="utf-8")
    return g

def test_snapshot_contains_god_nodes_and_communities(tmp_path):
    text = snapshot_text(_mini_graph_file(tmp_path))
    assert "alpha" in text and "core" in text      # 度数最高节点 + 社区标题
    assert len(text) < 3500                        # ~500 tok 上限（4 bytes/tok）

def test_rebuilding_window_marks_degraded(tmp_path):
    """零等待下恰处 rebuilding 窗口 -> 快照内容标 degraded（Q-1 裁决）."""
    state = tmp_path / "graphify-out" / ".rebuild-state.json"
    state.parent.mkdir(exist_ok=True)
    state.write_text(json.dumps({"schema": 1, "phase": "rebuilding",
                                 "started": time.time() - 5}), encoding="utf-8")
    text = snapshot_text(_mini_graph_file(tmp_path), state_path=state)
    assert "degraded" in text

def test_missing_graph_returns_empty_not_crash(tmp_path):
    assert snapshot_text(tmp_path / "nope.json") == ""
