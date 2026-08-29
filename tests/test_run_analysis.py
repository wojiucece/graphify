"""端到端：codegraph DB -> graphify-out（export.to_json 产物）."""
import json
from pathlib import Path

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
