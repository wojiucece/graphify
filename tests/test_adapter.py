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
