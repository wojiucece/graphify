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
