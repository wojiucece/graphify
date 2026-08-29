"""split_semantic_seed.py 测试（A1 links 键 + C2 hyperedges + C8 旧格式）."""
import json
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"
SRC = FIXTURES / "graph-legacy" / "semantic-seed-source.json"


def test_split_reads_links_not_edges():
    """A1: 真实旧图边在 links 键."""
    from split_semantic_seed import split
    stats = split(SRC, output_path=None)
    assert stats["seed_edges"] == 3  # fixture 有 3 条 semantic links


def test_split_carries_hyperedges():
    """C2: 顶层 hyperedges 整体携带."""
    from split_semantic_seed import split
    stats = split(SRC, output_path=None)
    assert len(stats["seed"]["hyperedges"]) == 1
    assert stats["seed"]["hyperedges"][0]["id"] == "auth_cluster"


def test_split_carries_semantic_nodes():
    from split_semantic_seed import split
    stats = split(SRC, output_path=None)
    ids = {n["id"] for n in stats["seed"]["nodes"]}
    assert "concept:auth" in ids and "rationale:why-jwt" in ids
    assert "file:docs/auth.md" in ids  # 锚点文件节点携带


def test_split_writes_output(tmp_path):
    from split_semantic_seed import split
    out = tmp_path / "seed.json"
    split(SRC, output_path=out)
    data = json.loads(out.read_text(encoding="utf-8"))
    assert "nodes" in data and "edges" in data and "hyperedges" in data
