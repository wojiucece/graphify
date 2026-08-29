"""Tests for adapter.py -- codegraph -> graphify 提取 schema 适配器."""
import sqlite3
import pytest
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"
MINI_DB = FIXTURES / "codegraph-fixtures" / "mini.codegraph.db"


def _make_db_with_version(path: Path, version: int):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE schema_versions (version INTEGER PRIMARY KEY, applied_at INTEGER, description TEXT)")
    conn.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY, kind TEXT, name TEXT, qualified_name TEXT, file_path TEXT, language TEXT, start_line INTEGER, end_line INTEGER, start_column INTEGER, end_column INTEGER, docstring TEXT, signature TEXT, visibility TEXT, is_exported INTEGER, is_async INTEGER, is_static INTEGER, is_abstract INTEGER, decorators TEXT, type_parameters TEXT, return_type TEXT, updated_at INTEGER)")
    conn.execute("CREATE TABLE edges (id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, target TEXT, kind TEXT, metadata TEXT, line INTEGER, col INTEGER, provenance TEXT)")
    conn.execute("CREATE TABLE files (path TEXT PRIMARY KEY, content_hash TEXT, language TEXT, size INTEGER, modified_at INTEGER, indexed_at INTEGER, node_count INTEGER, errors TEXT, generated INTEGER)")
    conn.execute("CREATE TABLE unresolved_refs (id INTEGER PRIMARY KEY AUTOINCREMENT, from_node_id TEXT, reference_name TEXT, reference_kind TEXT, line INTEGER, col INTEGER, candidates TEXT, file_path TEXT, language TEXT, status TEXT, name_tail TEXT)")
    conn.execute("INSERT INTO schema_versions VALUES (?, 0, 'fake')", (version,))
    conn.commit(); conn.close()


def test_version_gate_rejects_future_schema(tmp_path):
    from adapter import load_codegraph
    db = tmp_path / "future.db"
    _make_db_with_version(db, version=99)
    with pytest.raises(RuntimeError, match="schema.*99.*9"):
        load_codegraph(db, max_schema=9)


def test_load_maps_nodes_and_edges():
    from adapter import load_codegraph
    result = load_codegraph(MINI_DB, max_schema=9)
    assert len(result["nodes"]) > 0 and len(result["edges"]) > 0
    n = result["nodes"][0]
    assert set(n.keys()) >= {"id", "label", "source_file", "source_location", "kind", "language"}
    assert n["source_location"].startswith("L") and n["source_location"][1:].isdigit()
    e = result["edges"][0]
    assert set(e.keys()) >= {"source", "target", "relation", "confidence"}
    assert e["confidence"] in ("EXTRACTED", "INFERRED")


def test_load_includes_file_nodes():
    from adapter import load_codegraph
    result = load_codegraph(MINI_DB, max_schema=9)
    assert "file" in {n["kind"] for n in result["nodes"]}


def test_load_provenance_to_confidence():
    from adapter import load_codegraph
    result = load_codegraph(MINI_DB, max_schema=9)
    assert "EXTRACTED" in {e["confidence"] for e in result["edges"]}


def test_load_exports_knowledge_gaps(tmp_path):
    """unresolved_refs status='failed' -> knowledge_gaps（§3.2，Phase 0 实测 32354 条）."""
    from adapter import load_codegraph
    db = tmp_path / "kg.db"
    _make_db_with_version(db, version=9)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO nodes VALUES ('function:a','function','a','a','a.py','python',1,1,0,0,NULL,NULL,NULL,0,0,0,0,NULL,NULL,NULL,0)")
    conn.execute("INSERT INTO unresolved_refs (from_node_id,reference_name,reference_kind,line,col,file_path,language,status,name_tail) VALUES ('function:a','missing_fn','references',10,5,'a.py','python','failed','missing_fn')")
    conn.commit(); conn.close()
    result = load_codegraph(db, max_schema=9)
    gaps = result["knowledge_gaps"]
    assert len(gaps) == 1
    assert gaps[0]["ref"] == "missing_fn"
    assert gaps[0]["node"] == "function:a"


def test_knowledge_gaps_truncated_to_100(tmp_path):
    """failed 超 100 条截断（防报告爆炸）."""
    from adapter import load_codegraph
    db = tmp_path / "many.db"
    _make_db_with_version(db, version=9)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO nodes VALUES ('function:a','function','a','a','a.py','python',1,1,0,0,NULL,NULL,NULL,0,0,0,0,NULL,NULL,NULL,0)")
    for i in range(150):
        conn.execute("INSERT INTO unresolved_refs (from_node_id,reference_name,reference_kind,line,col,file_path,language,status,name_tail) VALUES ('function:a',?,'references',?,0,'a.py','python','failed',?)", (f"miss{i}", i, f"miss{i}"))
    conn.commit(); conn.close()
    assert len(load_codegraph(db, max_schema=9)["knowledge_gaps"]) == 100
