"""codegraph SQLite -> graphify 提取 schema 只读适配器（方案 §3）."""
from __future__ import annotations
import json as _json
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
        _disambiguate_ids(nodes, edges)
        return {
            "nodes": nodes,
            "edges": edges,
            "knowledge_gaps": _map_knowledge_gaps(conn),
        }
    finally:
        conn.close()


_KG_TOP_N = 100  # 防 report 爆炸（Phase 0 实测 32354 条 failed）


def _map_knowledge_gaps(conn):
    """unresolved_refs status='failed' -> Knowledge Gaps 输入（§3.2）."""
    out = []
    for from_node, ref_name, line, file_path in conn.execute(
        "SELECT from_node_id, reference_name, line, file_path "
        "FROM unresolved_refs WHERE status='failed' LIMIT ?",
        (_KG_TOP_N,)):
        out.append({"ref": ref_name, "node": from_node, "file": file_path, "line": line})
    return out


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


_GENERIC_RELATIONS = frozenset({"references", "uses", "mentions"})
_RELATION_PRIORITY = {"calls":10,"imports":9,"contains":8,"extends":7,
                      "implements":6,"instantiates":5,"decorates":4}


def _relation_rank(kind: str) -> int:
    return 0 if kind in _GENERIC_RELATIONS else _RELATION_PRIORITY.get(kind, 1)


def _map_edges(conn):
    pair_best = {}
    for source, target, kind, provenance, metadata in conn.execute(
        "SELECT source, target, kind, provenance, metadata FROM edges"):
        candidate = {"source": source, "target": target, "relation": kind,
                     "confidence": _PROVENANCE_CONFIDENCE.get(provenance or "", "EXTRACTED")}
        # C3: 展开 metadata，保留 synthesizedBy
        if metadata:
            try:
                md = _json.loads(metadata)
                if "synthesizedBy" in md:
                    candidate["synthesized_by"] = md["synthesizedBy"]
            except Exception:
                pass
        key = (source, target)
        existing = pair_best.get(key)
        if existing is None or _relation_rank(candidate["relation"]) > _relation_rank(existing["relation"]):
            # C3: 败者 synthesizedBy 合入胜者（数组并集，非单值覆盖）
            if existing and existing.get("synthesized_by"):
                sb = set(existing.get("synthesized_by_list", [existing["synthesized_by"]]))
                sb.update(candidate.get("synthesized_by_list", [candidate["synthesized_by"]] if candidate.get("synthesized_by") else []))
                candidate["synthesized_by_list"] = sorted(sb)
            pair_best[key] = candidate
        elif candidate.get("synthesized_by"):
            # 败者的 synthesizedBy 并入胜者数组
            sb = set(existing.get("synthesized_by_list", [existing["synthesized_by"]] if existing.get("synthesized_by") else []))
            sb.add(candidate["synthesized_by"])
            existing["synthesized_by_list"] = sorted(sb)
    return list(pair_best.values())


def _disambiguate_ids(nodes, edges):
    """normalize_id 后碰撞的 id 消歧，并同步 remap 边端点（A4）."""
    from graphify.ids import normalize_id
    groups = {}
    for idx, n in enumerate(nodes):
        groups.setdefault(normalize_id(n["id"]), []).append(idx)
    id_map = {}  # old_id -> new_id
    for norm, idxs in groups.items():
        if len(idxs) <= 1:
            continue
        for suffix, idx in enumerate(idxs):
            old = nodes[idx]["id"]
            new = f"{old}__cg{suffix}"
            nodes[idx]["id"] = new
            id_map[old] = new
    if not id_map:
        return
    for e in edges:
        e["source"] = id_map.get(e["source"], e["source"])
        e["target"] = id_map.get(e["target"], e["target"])
