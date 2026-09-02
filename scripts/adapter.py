"""codegraph SQLite -> graphify 提取 schema 只读适配器（方案 §3）."""
from __future__ import annotations
import json as _json
import sqlite3
import sys
from pathlib import Path
from typing import Any

DEFAULT_MAX_SCHEMA = 9  # Phase 0 实测：codegraph 1.6.0 = schema v9


def _open_readonly(db_path: str | Path, allow_immutable_fallback: bool = False) -> sqlite3.Connection:
    """URI 只读连接。WAL 下 immutable=1 会漏读未 checkpoint 数据（§3.3），正常路径禁用；
    仅当 mode=ro 打不开（只读介质缺 -shm）且 allow_immutable_fallback=True 时降级 immutable——
    只读介质无并发写，陈旧快照顾虑不成立（A3 降级链 Q11）。降级写日志。"""
    p = Path(db_path).resolve()
    try:
        return sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        if not allow_immutable_fallback:
            raise
        print(f"[adapter] mode=ro 打开失败，降级 immutable=1: {p}", file=sys.stderr)
        return sqlite3.connect(f"file:{p.as_posix()}?immutable=1", uri=True)


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
        conn.execute("BEGIN")  # A3：单事务快照读——WAL 下并发写期间所有 SELECT 恒读事务开始时的快照
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
    # L1（用户审查）：codegraph edges 表无文件列，LEFT JOIN 取 source 端点 file_path
    # 补边 source_file（方案 §3.2 映射缺口，消 build REQUIRED_EDGE_FIELDS 告警）。
    # validate.py 对边只查键存在（缺失键才告警），None 值不告警，故端点缺失的
    # dangling 边经 LEFT JOIN 保留 source_file=None 不丢键；build 图阶段对 falsy
    # 边 source_file 本就回退端点节点值兜底（build.py 边循环 if not attrs.get("source_file")）。
    for source, target, kind, provenance, metadata, src_file in conn.execute(
        "SELECT e.source, e.target, e.kind, e.provenance, e.metadata, n.file_path "
        "FROM edges e LEFT JOIN nodes n ON e.source = n.id"):
        candidate = {"source": source, "target": target, "relation": kind,
                     "confidence": _PROVENANCE_CONFIDENCE.get(provenance or "", "EXTRACTED"),
                     "source_file": src_file}
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
            # M-b（最终审查）：existing 可能只有并集后的 synthesized_by_list 而无单值键
            # （此前更高 rank 边置换时生成）——get 的默认参数急切求值，
            # [existing["synthesized_by"]] 在无单值键时会 KeyError，须分步取。
            if existing and (existing.get("synthesized_by") or existing.get("synthesized_by_list")):
                sb = set(existing.get("synthesized_by_list") or [])
                if existing.get("synthesized_by"):
                    sb.add(existing["synthesized_by"])
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


_SEMANTIC_FILE_TYPES = frozenset({"concept", "document", "rationale", "image"})


def validate_semantic_anchors(seed: dict) -> list[str]:
    """semantic 引用必须锚定文件级节点（§6.3 Q2）.
    B2: 真实种子无 kind，按 id 前缀判定--file: 前缀或 file_type 在语义集为合规锚点；
    codegraph 符号 id（kind:hash 形态）判违规（符号移动行号即变 id，易失）."""
    node_info = {n["id"]: n for n in seed.get("nodes", [])}
    violations = []
    for e in seed.get("edges", []):
        for end in ("source", "target"):
            nid = e.get(end, "")
            n = node_info.get(nid, {})
            # semantic 自身节点不判违规
            if nid.startswith(("concept:", "rationale:", "document:", "image:")):
                continue
            if n.get("_origin") == "semantic" or n.get("file_type") in _SEMANTIC_FILE_TYPES:
                continue
            # 合规锚点：file: 前缀 或 kind=file
            if nid.startswith("file:") or n.get("kind") == "file":
                continue
            # 其余为符号级（codegraph kind:hash 形态）-> 违规
            violations.append(f"semantic 边 {end} 锚定符号级节点 {nid}（易失），应锚定文件级节点")
    return violations


# === B3 分发线索直查（Phase 4 Task 9）=============================================
# serve.py 持自包含副本（graphify 包不反向依赖 scripts/；rebuild_entry._parse_refresh
# "scripts 无包结构，两文件各持自包含副本，不互相导入" 先例）——改这里要同步 serve 副本，
# 防漂移锁定靠 tests/test_dispatch_trace.py::test_serve_selfcontained_copies_match_adapter。

_DISPATCH_RESOLVED_BY = frozenset({"instance-method", "fuzzy", "qualified-name"})


def _dispatch_candidate(meta: dict | None) -> bool:
    """B3 R4-2 双判据并集：resolvedBy ∈ {instance-method, fuzzy, qualified-name, None}
    或 confidence < 0.9 → 分发候选（宁多标勿漏标）。无 metadata = resolvedBy None 分支
    （未记录解析方式 = 不可信）→ True。"""
    if not meta:
        return True
    if meta.get("resolvedBy") in _DISPATCH_RESOLVED_BY or meta.get("resolvedBy") is None:
        return True
    conf = meta.get("confidence")
    try:
        if conf is not None and float(conf) < 0.9:
            return True
    except (TypeError, ValueError):
        pass
    return False


def _symbol_short_name(d: dict, nid: str) -> str:
    """B3 R3-2 合并图短名：label 首 token 末段（消歧后缀 ' (N)' 剥离）。
    合并图 id 是 hash 形态且无 name 属性——短名唯一事实源是 label；label 缺失回退 nid。"""
    return str(d.get("label") or nid).split(" ")[0].rsplit(".", 1)[-1]


def _edge_dispatch_info(conn, source_file, kind, src_name, tgt_name) -> dict:
    """B3 R4-2 铰链改键：合并边无 DB edge id（实测生产 links 为 {source, target, relation,
    _origin, confidence, ...}，且合并边 source 是 folded id、DB 边端点是 raw id、fold 多对一
    不留反映射）——`WHERE edge_id=?` 无从落地。改用合并边自带的 source_file + relation
    （→ DB kind）+ 两端短名（nodes.name 即短名，_symbol_short_name ↔ nodes.name）在 DB 侧
    JOIN 定候选组（file+kind+双端短名缩到个位数）。组内最保守取值（Q11：不洗白猜测边）：
    confidence=min、dispatch_candidate=any、resolvedBy 数组展示全部。空组 → dispatch_candidate
    =True 宁多标勿漏标。
    M2（review 注释登记，已知模糊面）：同名重载并组——同文件同 kind 同短名对的候选组会
    把其他重载方法的 min confidence / resolvedBy 灌入。宁多标方向：保守可接受（Q11）。"""
    rows = conn.execute(
        "SELECT e.metadata FROM edges e "
        "JOIN nodes ns ON e.source = ns.id "
        "JOIN nodes nt ON e.target = nt.id "
        "WHERE e.kind = ? AND ns.file_path = ? AND ns.name = ? AND nt.name = ?",
        (kind, source_file, src_name, tgt_name)).fetchall()
    resolved_by = []
    confidences = []
    candidate = False
    for (meta_json,) in rows:
        meta = None
        if meta_json:
            try:
                meta = _json.loads(meta_json)
            except ValueError:
                meta = None
        candidate = candidate or _dispatch_candidate(meta)
        if meta:
            rb = meta.get("resolvedBy")
            if rb is not None and rb not in resolved_by:
                resolved_by.append(rb)
            conf = meta.get("confidence")
            if conf is not None:
                try:
                    confidences.append(float(conf))
                except (TypeError, ValueError):
                    pass
    if not rows:
        return {"resolvedBy": [], "confidence": None, "dispatch_candidate": True}
    return {
        "resolvedBy": resolved_by,
        "confidence": min(confidences) if confidences else None,
        "dispatch_candidate": candidate,
    }
