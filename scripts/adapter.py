"""codegraph SQLite -> graphify 提取 schema 只读适配器（方案 §3）."""
from __future__ import annotations
import sqlite3
from pathlib import Path
from typing import Any

DEFAULT_MAX_SCHEMA = 9  # Phase 0 实测：codegraph 1.6.0 = schema v9


def _open_readonly(db_path: str | Path) -> sqlite3.Connection:
    """URI 只读连接，禁用 immutable=1（WAL 下漏读未 checkpoint 数据，§3.3）."""
    p = Path(db_path).resolve()
    return sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)


def _check_schema(conn: sqlite3.Connection, max_schema: int) -> int:
    row = conn.execute("SELECT MAX(version) FROM schema_versions").fetchone()
    version = row[0] if row else 0
    if version > max_schema:
        raise RuntimeError(
            f"codegraph schema 版本 {version} 高于适配器已测试上限 {max_schema}，"
            f"请核对 scripts/adapter.py 映射表与方案 §3.2 后提升 max_schema")
    return version


def load_codegraph(db_path: str | Path, max_schema: int = DEFAULT_MAX_SCHEMA) -> dict[str, Any]:
    conn = _open_readonly(db_path)
    try:
        _check_schema(conn, max_schema)
        nodes = _map_nodes(conn)
        edges = _map_edges(conn)
        return {"nodes": nodes, "edges": edges}
    finally:
        conn.close()


_PROVENANCE_CONFIDENCE = {"heuristic": "INFERRED"}


def _map_nodes(conn):
    out = []
    for nid, qname, name, file_path, language, start_line, kind in conn.execute(
        # COALESCE(start_line,1)：file 节点实测 start_line=1，但防御 NULL -> "LNone" 破坏 ^L\d 判层
        "SELECT id, qualified_name, name, file_path, language, COALESCE(start_line,1), kind FROM nodes"):
        out.append({
            "id": nid,
            "label": qname if qname else name,  # qualified_name 优先（§3.2）
            "source_file": file_path,
            "source_location": f"L{start_line}",  # ^L\d 判 AST 层
            "kind": kind,
            "language": language,
        })
    return out


def _map_edges(conn):
    out = []
    for source, target, kind, provenance, metadata in conn.execute(
        "SELECT source, target, kind, provenance, metadata FROM edges"):
        out.append({
            "source": source, "target": target,
            "relation": kind,
            "confidence": _PROVENANCE_CONFIDENCE.get(provenance or "", "EXTRACTED"),
        })
    return out
