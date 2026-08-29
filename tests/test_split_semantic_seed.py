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


def test_split_remaps_py_anchor_by_source_file(tmp_path):
    """B6: 旧图锚点 file:src/app.py 改指 codegraph 文件节点 file:app.py（source_file 匹配，旧≠新）.
    真实场景：旧图 id 是归一化形式，codegraph id 是 file:<path>."""
    from split_semantic_seed import split
    # 造一个旧图：semantic 边锚到 file:src/app.py（归一化 id）
    old = {
        "nodes": [
            {"id": "file:src/app.py", "label": "app.py", "source_file": "src/app.py", "_origin": "ast", "file_type": "code"},
            {"id": "concept:x", "label": "X", "_origin": "semantic", "file_type": "concept"},
        ],
        "links": [{"source": "concept:x", "target": "file:src/app.py", "relation": "documents", "_origin": "semantic"}],
        "hyperedges": [],
    }
    old_path = tmp_path / "old.json"
    old_path.write_text(__import__("json").dumps(old), encoding="utf-8")
    # codegraph DB：file 节点 id 是 file:app.py（路径差异）
    import sqlite3
    db = tmp_path / "cg.db"
    _make_cg_db(db)
    stats = split(old_path, output_path=None, codegraph_db=db)
    seed = stats["seed"]
    # 锚点 id 改指为 codegraph id（seed 中非锚点节点无 source_file 键，用 get）
    remapped = [n for n in seed["nodes"] if n.get("source_file") == "src/app.py"]
    assert remapped and remapped[0]["id"] == "file:app.py", f"锚点未改指: {remapped}"
    # 边端点同步 remap
    edge = seed["edges"][0]
    assert edge["target"] == "file:app.py", f"边端点未 remap: {edge}"
    assert stats["anchor_remap"] == 1


def _make_cg_db(db):
    import sqlite3
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE schema_versions (version INTEGER PRIMARY KEY, applied_at INTEGER, description TEXT)")
    conn.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY, kind TEXT, name TEXT, qualified_name TEXT, file_path TEXT, language TEXT, start_line INTEGER, end_line INTEGER, start_column INTEGER, end_column INTEGER, docstring TEXT, signature TEXT, visibility TEXT, is_exported INTEGER, is_async INTEGER, is_static INTEGER, is_abstract INTEGER, decorators TEXT, type_parameters TEXT, return_type TEXT, updated_at INTEGER)")
    conn.execute("INSERT INTO schema_versions VALUES (9,0,'fake')")
    conn.execute("INSERT INTO nodes VALUES ('file:app.py','file','app.py','app.py','src/app.py','python',1,1,0,0,NULL,NULL,NULL,0,0,0,0,NULL,NULL,NULL,0)")
    conn.commit(); conn.close()
