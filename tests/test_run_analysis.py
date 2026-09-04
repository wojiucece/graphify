"""analysis-only：读事实层 mini graph.json -> GRAPH_REPORT.md / knowledge-gaps.json.

Task 09 换源：输入从 mini.codegraph.db（codegraph 退役）换成 mini-graph.json（事实层
node_link 格式 + failed_refs）。seed/refresh/shrink-guard 等 build 步骤已随 rebuild_entry
编排迁至 test_rebuild_entry.py。
"""
import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
MINI_GRAPH = FIXTURES / "mini-graph.json"


def test_run_reads_graph_and_writes_report(tmp_path):
    """读事实层 graph.json -> GRAPH_REPORT.md（graph.json 是输入，已存在）."""
    from run_analysis import run
    out = run(MINI_GRAPH, output_dir=tmp_path, root="fixture-src")
    out = Path(out)
    report = out / "GRAPH_REPORT.md"
    assert report.exists()
    assert report.stat().st_size > 100


def test_run_writes_knowledge_gaps_sidecar(tmp_path):
    """failed_refs（Task 04 收集器，Task 07 持久化）-> knowledge-gaps.json sidecar.
    mini-graph.json 含 1 条 failed ref（from file:c.py -> 'b'），sidecar 必须可解析
    且含该条——{ref, node, file, line} 形态与旧 adapter unresolved_refs 输出同构."""
    from run_analysis import run
    out = run(MINI_GRAPH, output_dir=tmp_path, root="fixture-src")
    kg_path = Path(out) / "knowledge-gaps.json"
    assert kg_path.exists(), "knowledge-gaps.json sidecar 未写"
    gaps = json.loads(kg_path.read_text(encoding="utf-8"))
    assert isinstance(gaps, list) and len(gaps) >= 1
    assert set(gaps[0].keys()) >= {"ref", "node", "file", "line"}
    assert gaps[0]["ref"] == "b"
    assert gaps[0]["node"] == "file:c.py" and gaps[0]["file"] == "c.py"


def test_run_no_failed_refs_writes_empty_list(tmp_path):
    """事实层无 failed_refs -> knowledge-gaps.json 空数组（诚实空，非缺失）."""
    import copy
    from run_analysis import run
    g = json.loads(MINI_GRAPH.read_text(encoding="utf-8"))
    g.pop("failed_refs", None)
    path = tmp_path / "no-gaps.json"
    path.write_text(json.dumps(g), encoding="utf-8")
    out = run(path, output_dir=tmp_path / "out", root="fixture-src")
    gaps = json.loads((Path(out) / "knowledge-gaps.json").read_text(encoding="utf-8"))
    assert gaps == []


def test_run_missing_graph_raises(tmp_path):
    """事实层缺失 -> FileNotFoundError（诚实暴露，不静默降级）."""
    from run_analysis import run
    with pytest.raises(FileNotFoundError):
        run(tmp_path / "nonexistent.json", output_dir=tmp_path / "out")


def test_run_wiki_generates_obsidian(tmp_path):
    """wiki=True -> Obsidian 出口（to_obsidian 照旧）."""
    from run_analysis import run
    out = Path(run(MINI_GRAPH, output_dir=tmp_path / "out", root="fixture-src", wiki=True))
    assert (out / "wiki").exists()


def test_surprising_connections_always_passes_communities(monkeypatch, tmp_path):
    """B3: run_analysis 调 surprising_connections 必传 communities -> betweenness 死路径(L358)不可达.
    关键：patch run_analysis 模块持有的引用（run_analysis 顶部 from graphify.analyze import
    surprising_connections 已绑定进自己的全局；patch graphify.analyze.surprising_connections
    不影响 run_analysis 持有的引用 -> captured 恒空 -> 断言必失败）。这是 D2 规则要防的错误。"""
    import run_analysis
    from run_analysis import run
    captured = {}

    def fake_sc(G, communities=None, top_n=10):
        captured["communities"] = communities
        return []
    monkeypatch.setattr(run_analysis, "surprising_connections", fake_sc)
    run(MINI_GRAPH, output_dir=tmp_path, root="fixture-src")
    assert captured["communities"] is not None, "必须恒传 communities（锁死 betweenness 死路径）"


def test_large_graph_end_to_end_not_timeout(tmp_path):
    """B3②: 大图(>1000节点)端到端不超时（surprising_connections 采样 + god_nodes 度数排序的行为证据）.
    事实层 graph.json 输入（新链路形态）；无大图事实层（golden 根未重建）则跳过。"""
    import os, time
    root = Path(os.environ.get("GRAPHIFY_GOLDEN_ROOT", "D:/code/graphify_fork"))
    graph = root / "graphify-out" / "graph.json"
    if not graph.exists():
        pytest.skip("无大图事实层 graph.json（GRAPHIFY_GOLDEN_ROOT 未重建）")
    from run_analysis import run
    t0 = time.monotonic()
    out = run(graph, output_dir=tmp_path / "out", root=str(root))
    elapsed = time.monotonic() - t0
    assert (Path(out) / "GRAPH_REPORT.md").exists()
    assert elapsed < 120, f"大图分析耗时 {elapsed:.1f}s 异常，可能采样未生效"
