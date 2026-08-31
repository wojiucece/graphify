"""端到端：codegraph DB -> graphify-out（export.to_json 产物）."""
import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
MINI_DB = FIXTURES / "codegraph-fixtures" / "mini.codegraph.db"


def test_run_produces_node_link_graph_and_report(tmp_path):
    from run_analysis import run
    out = run(MINI_DB, output_dir=tmp_path, root="fixture-src")
    out = Path(out)
    graph = out / "graph.json"
    report = out / "GRAPH_REPORT.md"
    assert graph.exists() and report.exists()
    data = json.loads(graph.read_text(encoding="utf-8"))
    # B4: node_link 格式 -> links 键（非 edges）
    assert "links" in data, "graph.json 必须是 node_link 格式（links 键），serve.py 依赖"
    assert "nodes" in data and len(data["nodes"]) > 0
    assert report.stat().st_size > 100


def test_run_merges_seed_and_attaches_hyperedges(tmp_path):
    from run_analysis import run
    seed = {
        "nodes": [{"id": "concept:auth", "label": "认证", "_origin": "semantic", "file_type": "concept"}],
        "edges": [{"source": "concept:auth", "target": "file:fixture-src/a.py", "relation": "documents"}],
        "hyperedges": [{"id": "auth_cluster", "label": "认证簇", "nodes": ["concept:auth"]}],
    }
    seed_path = tmp_path / "seed.json"
    seed_path.write_text(__import__("json").dumps(seed), encoding="utf-8")
    out = run(MINI_DB, output_dir=tmp_path / "out", root="fixture-src", semantic_seed=seed_path)
    data = __import__("json").loads((Path(out) / "graph.json").read_text(encoding="utf-8"))
    ids = {n["id"] for n in data["nodes"]}
    assert "concept:auth" in ids
    # C2: hyperedges 挂回（node_link 格式 graph.hyperedges 或顶层 hyperedges）
    hes = data.get("hyperedges") or data.get("graph", {}).get("hyperedges") or []
    assert any(h["id"] == "auth_cluster" for h in hes), "hyperedges 未挂回"


def test_run_warns_on_symbolic_anchor_violations(tmp_path, capfd):
    """I1/B2 接线（最终审查）：seed 含符号级锚点（function:xxx）-> stderr 告警但不阻断.
    validate_semantic_anchors 原仅测试调用；接线后 run() 内消费（违规只提示，不跑不炸）。"""
    from run_analysis import run
    seed = {
        "nodes": [{"id": "concept:auth", "label": "认证", "_origin": "semantic", "file_type": "concept"}],
        "edges": [{"source": "concept:auth", "target": "function:abc123", "relation": "documents"}],
        "hyperedges": [],
    }
    seed_path = tmp_path / "seed.json"
    seed_path.write_text(json.dumps(seed), encoding="utf-8")
    out = run(MINI_DB, output_dir=tmp_path / "out", root="fixture-src", semantic_seed=seed_path)
    captured = capfd.readouterr()
    assert "语义锚定违规" in captured.err, f"stderr 未含锚定违规告警: {captured.err!r}"
    assert "function:abc123" in captured.err
    # 不阻断：run 正常产出
    assert (Path(out) / "graph.json").exists()


def test_run_writes_knowledge_gaps_sidecar(tmp_path):
    """I1/C4 接线（最终审查）：knowledge_gaps 写 <out>/knowledge-gaps.json.
    load_codegraph 返回的 knowledge_gaps 原零消费；fixture DB 有 1 条 failed ref
    （from file:c.py -> 'b'），sidecar 必须可解析且含该条。"""
    from run_analysis import run
    out = run(MINI_DB, output_dir=tmp_path, root="fixture-src")
    kg_path = Path(out) / "knowledge-gaps.json"
    assert kg_path.exists(), "knowledge-gaps.json sidecar 未写"
    gaps = json.loads(kg_path.read_text(encoding="utf-8"))
    assert isinstance(gaps, list) and len(gaps) >= 1
    assert set(gaps[0].keys()) >= {"ref", "node", "file", "line"}
    assert gaps[0]["ref"] == "b"


def test_run_shrink_guard_blocks_silent_overwrite(tmp_path):
    """审查 fix（Important）：to_json force=False 恢复 #479 shrink-guard。

    第二次 run() 面对"现有图节点数 > 新图"（缩量场景，如 seed/refresh 丢失）
    时必须 raise RuntimeError，且 graph.json 保持旧内容不被覆盖。
    """
    from run_analysis import run
    out = Path(run(MINI_DB, output_dir=tmp_path / "out", root="fixture-src"))
    graph = out / "graph.json"
    first_n = len(json.loads(graph.read_text(encoding="utf-8"))["nodes"])
    assert first_n > 0
    # 模拟缩量：把现有 graph.json 换成节点更多的伪造 node_link JSON，
    # 使 existing_n > new_n，触发 to_json 的 shrink-guard（export.py:267）。
    fake = {
        "nodes": [{"id": f"fake:{i}", "label": f"fake{i}"} for i in range(first_n + 50)],
        "links": [],
    }
    graph.write_text(__import__("json").dumps(fake), encoding="utf-8")
    with pytest.raises(RuntimeError, match="shrink-guard"):
        run(MINI_DB, output_dir=out, root="fixture-src")
    # graph.json 未被覆盖：仍是伪造内容（缩量写入被拦截）
    assert json.loads(graph.read_text(encoding="utf-8")) == fake


import time


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
    # patch run_analysis 模块持有的引用（非 graphify.analyze.surprising_connections）
    monkeypatch.setattr(run_analysis, "surprising_connections", fake_sc)
    run(MINI_DB, output_dir=tmp_path, root="fixture-src")
    assert captured["communities"] is not None, "必须恒传 communities（锁死 betweenness 死路径）"


def test_large_graph_end_to_end_not_timeout(tmp_path):
    """B3②: 大图(>1000节点)端到端不超时（surprising_connections 采样 + god_nodes 度数排序的行为证据）.
    不直测 suggest_questions（report.py 不调用它）；改为跑全管线断言产物存在且不超时。"""
    import os
    db = os.environ.get("CG_SMOKE_DB",
        "D:/code/graphify_fork/.worktrees/feat-codegraph-merge/.codegraph/codegraph.db")
    if not Path(db).exists():
        import pytest; pytest.skip("无大图 DB")
    from run_analysis import run
    t0 = time.monotonic()
    out = run(db, output_dir=tmp_path / "out", root="graphify")
    elapsed = time.monotonic() - t0
    assert (Path(out) / "GRAPH_REPORT.md").exists()
    # 行为断言：10k 节点量级应在 120s 内完成（采样生效证据，非硬超时上限）
    assert elapsed < 120, f"大图分析耗时 {elapsed:.1f}s 异常，可能采样未生效"
