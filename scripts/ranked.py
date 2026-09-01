"""B1 融合检索：FTS5 × 精确名 pinning × 结构 × 预算 四通道（只读直查 codegraph DB + 合并图）。

分层原理（docs/graphify-codegraph-phase4-plan.md §4-B1）：词法检索（FTS5 BM25 / 精确名
pinning）直查 `.codegraph/codegraph.db`——codegraph 符号事实层；结构（度数中心性）走
`graphify-out/graph.json` 合并图——graphify 分析层口径（合并图口径 ≠ DB GROUP BY，
折叠/remap 后度数不同）。token 级分流（Q8）：identifier tokens 进 FTS/pinning，
CJK tokens 进图侧子串（R18：纯中文 FTS 必零命中，显式报告 cjk_hits 杜绝"没搜到=不存在"）。

返回 dict：{"results": [{"id","label","score","stage","source_tool"}...],
           "query_shape": {...}, "gap_hit": bool}。serve 闭包用 format_ranked 序列化为
JSON 响应体（B1 是 JSON 工具），再经 N1 信封（serve.py `_apply_envelope`）装配
verdict/freshness。工具侧诚实自报（query_shape/count_mode/collisions/gap_hit）经
format_ranked 归入响应体 `_meta` 对象（方案 §4-B1：`_meta.query_shape` 报告）。
"""
from __future__ import annotations
import json, re, sqlite3, sys
from pathlib import Path

_SELF = Path(__file__).resolve().parent
if str(_SELF) not in sys.path:        # scripts 无包结构——同目录 import adapter（rebuild_entry 先例）
    sys.path.insert(0, str(_SELF))

from adapter import _open_readonly
# Q6（v1.7 裁决）：id 折叠复用同一实现，不复刻——adapter._disambiguate_ids 同款写法
# （graphify.ids.normalize_id，NFKC+casefold 折叠；brief 所述 adapter._normalize_id 系
# 同源别名，adapter 未导出该名，故直接从 graphify.ids 取同函数）。
from graphify.ids import normalize_id as _normalize_id

_FTS_LIMIT = 40      # FTS5 BM25 候选池上限
_PIN_LIMIT = 5       # 精确名 pinning 每 token 上限（R3-5：lower(name) 全扫，LIMIT 5）
_GAP_FILES_TOP = 3   # gap 摘要关联文件 top3
_FTS_SCORE_DECAY = 1.0
_PINNED_SCORE = 1.0
_CJK_SCORE = 0.4

_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|[\u4e00-\u9fff]+")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
# Q6 \u78b0\u649e\u5224\u5b9a\uff1aadapter._disambiguate_ids \u6d88\u6b67\u540e\u7f00\u4e3a `__cg{n}`\uff0c\u7ecf _normalize_id \u6298\u53e0
# \uff08NFKC+casefold\uff0c\u8fde\u7eed\u4e0b\u5212\u7ebf\u584c\u7f29\u4e3a\u5355\u7ebf\uff09\u540e\u4e3a `_cg{n}`\u2014\u2014\u7528\u6298\u53e0\u540e\u5f62\u6001\u68c0\u6d4b\uff08ruling \u539f\u6587
# "fold \u7ed3\u679c\u542b\u6d88\u6b67\u540e\u7f00"\uff09\uff0c\u5e26\u5c3e\u6570\u5b57\u9632\u81ea\u7136\u540d `foo_cg` \u8bef\u5224\u3002
_CG_SUFFIX_RE = re.compile(r"_cg\d+$")

_ALL_CHANNELS = frozenset({"fts", "pinned", "cjk", "structure"})

_tiktoken_enc = None  # 可选依赖惰性缓存：None=未探测 / False=不可用 / 其余=编码器


def _count_tokens(text: str) -> tuple[int, str]:
    """token 计数：tiktoken 可用则精确（optional extra），否则 len//4 估算（declared，§8 ~4B/token）。"""
    global _tiktoken_enc
    if _tiktoken_enc is None:
        try:
            import tiktoken
            _tiktoken_enc = tiktoken.get_encoding("cl100k_base")
        except Exception:
            _tiktoken_enc = False
    if _tiktoken_enc is not False:
        return len(_tiktoken_enc.encode(text)), "tiktoken"
    return len(text) // 4, "estimate"


def _split_tokens(query: str) -> tuple[list[str], list[str]]:
    """token 级分流（Q8）：identifier tokens 进 FTS/pinning，CJK tokens 进图侧子串。"""
    identifiers, cjk = [], []
    for m in _TOKEN_RE.finditer(query):
        t = m.group()
        (cjk if _CJK_RE.fullmatch(t) else identifiers).append(t)
    return identifiers, cjk


def _is_source_shaped(tok: str) -> bool:
    """source-shaped token：snake_case（含 _）/ CamelCase / PascalCase（大小写切换）。"""
    if "_" in tok:
        return True
    return any(a.islower() and b.isupper() for a, b in zip(tok, tok[1:]))


def _fts_search(conn, identifiers: list[str]):
    """FTS5 BM25 通道（brief 全参：5 列权重 id=0/name=3/qn=2/doc=0.2/sig=1——id 列权重=0
    是设计选择非漏参：id 是 raw hash 无语义，匹配无检索价值）。返回 (rows, hits)，
    rows = (id, name, qualified_name, snippet, bm25)。nodes_fts 缺失/语法异常 → 诚实零命中。"""
    if not identifiers:
        return [], 0
    try:
        match = " ".join(f'"{t}"' for t in identifiers)   # 引号防 FTS 语法注入；隐式 AND
        rows = conn.execute(
            "SELECT id, name, qualified_name, "
            "COALESCE(snippet(nodes_fts, 3, '', '', '…', 8), '') AS snip, "
            "bm25(nodes_fts, 0, 3, 2, 0.2, 1) AS score "
            "FROM nodes_fts WHERE nodes_fts MATCH ? ORDER BY score LIMIT ?",
            (match, _FTS_LIMIT)).fetchall()
        return rows, len(rows)
    except sqlite3.OperationalError:
        return [], 0


def _pinning_search(conn, source_shaped: list[str]):
    """精确名 pinning 通道（R3-5：schema 无 lower_name 列，用 lower(name) 表达式全扫，
    LIMIT 5 上限）。返回 (rows, pinned_terms)，rows = (id, qualified_name, name)。"""
    rows, pinned_terms = [], []
    for tok in source_shaped:
        try:
            found = conn.execute(
                "SELECT id, qualified_name, name FROM nodes WHERE lower(name) = ? LIMIT ?",
                (tok.lower(), _PIN_LIMIT)).fetchall()
        except sqlite3.OperationalError:
            found = []
        if found:
            pinned_terms.append(tok)
            rows.extend(found)
    return rows, pinned_terms


def _gap_refs(conn):
    """Knowledge Gaps：unresolved_refs status='failed' → {ref.lower(): {ref, files[top3]}}。
    表缺失（旧 schema/合成 fixture）→ 诚实空（R18：没搜到 ≠ 不存在）。"""
    out = {}
    try:
        rows = conn.execute(
            "SELECT reference_name, file_path, line "
            "FROM unresolved_refs WHERE status='failed'").fetchall()
    except sqlite3.OperationalError:
        return out
    for ref, file, line in rows:
        if not ref:
            continue
        key = ref.lower()
        entry = out.setdefault(key, {"ref": ref, "files": []})
        if file and len(entry["files"]) < _GAP_FILES_TOP:
            entry["files"].append(f"{file}:{line}" if line else file)
    return out


def _load_graph(graph_path: Path):
    """合并图加载 → (degree_map, collision_bases, nodes)。加载失败 → (None, None, [])。
    degree：links 端点计数（合并图口径）。collision_bases：_normalize_id fold 含消歧
    后缀（__cg）的图节点原始 id 基底（Q6：碰撞节点不参与 join）。"""
    try:
        g = json.loads(graph_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, None, []
    degree = {}
    for link in g.get("links", []):
        s, t = link.get("source"), link.get("target")
        if s:
            degree[s] = degree.get(s, 0) + 1
        if t:
            degree[t] = degree.get(t, 0) + 1
    collision_bases = set()
    for n in g.get("nodes", []):
        nid = n.get("id", "")
        if nid and _CG_SUFFIX_RE.search(_normalize_id(nid)):
            collision_bases.add(nid.split("__cg")[0])
    return degree, collision_bases, g.get("nodes", [])


def _cjk_search(nodes, cjk_tokens: list[str]):
    """CJK 通道：合并图 label 子串匹配（O(n)，语义节点规模可行）。返回 (rows, hits)，
    rows = (id, label)。一节点命中任一 CJK token 计一次（跨 token 去重）。"""
    rows, hits = [], 0
    if not cjk_tokens:
        return rows, hits
    for node in nodes:
        label = node.get("label") or ""
        if label and any(tok in label for tok in cjk_tokens):
            hits += 1
            rows.append((node.get("id", ""), label))
    return rows, hits


def _db_degree(conn):
    """降级路径：DB edges GROUP BY 度数（合并图不可用，SQL GROUP BY 口径）。"""
    degree = {}
    try:
        for (s, t) in conn.execute("SELECT source, target FROM edges"):
            if s:
                degree[s] = degree.get(s, 0) + 1
            if t:
                degree[t] = degree.get(t, 0) + 1
    except sqlite3.OperationalError:
        pass
    return degree


def ranked_context(root, query: str, token_budget: int = 2000, channels=None) -> dict:
    """四通道融合检索。

    root: 项目根（含 .codegraph/codegraph.db + graphify-out/graph.json）。
    channels: 启用通道子集（None=四通道全开）；"BM25-only" 对照（金标集）传
    {"fts"}——通道关闭时结构信号不参与（BM25-only 口径无度数 tiebreak）。
    """
    root = Path(root)
    active = _ALL_CHANNELS if channels is None else frozenset(channels)
    identifiers, cjk = _split_tokens(query)
    shape = {
        "tokens": {"identifier": identifiers, "cjk": cjk},
        "fts_hits": 0,
        "cjk_hits": 0,
        "pinned": [],
        "scanned": 0,
        "centrality": "disabled" if "structure" not in active else "merged_graph",
        "count_mode": "estimate",
        "collisions": 0,
        "gap_summary": [],
    }

    conn = _open_readonly(root / ".codegraph" / "codegraph.db")
    try:
        conn.execute("BEGIN")   # 单事务快照读（Task 4 模式，WAL 并发写下恒读一致快照）
        fts_rows, fts_hits = (_fts_search(conn, identifiers) if "fts" in active else ([], 0))
        pinned_rows, pinned_terms = (
            _pinning_search(conn, [t for t in identifiers if _is_source_shaped(t)])
            if "pinned" in active else ([], []))
        shape["fts_hits"] = fts_hits
        shape["pinned"] = pinned_terms
        gap_refs = _gap_refs(conn)

        degree, collision_bases, nodes = None, set(), []
        if "structure" in active or "cjk" in active:
            degree, collision_bases, nodes = _load_graph(root / "graphify-out" / "graph.json")
            if degree is None:      # 图加载失败 → 结构降级 DB GROUP BY 口径
                shape["centrality"] = "raw_db"
                degree = _db_degree(conn)
                collision_bases = set()
        if "structure" not in active:
            degree = None           # BM25-only 对照：结构信号全关（无度数 tiebreak）
        cjk_rows, cjk_hits = (_cjk_search(nodes, cjk) if "cjk" in active else ([], 0))
        shape["cjk_hits"] = cjk_hits
    finally:
        conn.close()

    # 融合装配：stage 优先级（pinned > fts > cjk）+ 通道内排序 + 结构度数平局打破
    results, seen = [], set()

    def _add(item: dict) -> None:
        iid = item["id"]
        if iid in seen:
            return
        seen.add(iid)
        d = 0
        if degree is not None:
            if iid in collision_bases:   # 碰撞节点不参与 join（Q6），记录不误当真实度 0
                shape["collisions"] += 1
            else:
                d = degree.get(iid, 0)
        item["degree"] = d
        results.append(item)

    for pid, qn, name in pinned_rows:   # ① 精确名（最高置信，exact name）
        _add({"id": pid, "label": qn or name, "score": _PINNED_SCORE,
              "stage": "pinned", "source_tool": "exact_name"})
    # ② FTS5 BM25（bm25 主序，score 随 rank 几何衰减；度数作平局打破——结构通道）
    fts_sorted = (sorted(fts_rows, key=lambda r: (r[4], -degree.get(r[0], 0)))
                  if degree is not None else fts_rows)
    for i, (fid, name, qn, _snip, _bm) in enumerate(fts_sorted):
        _add({"id": fid, "label": qn or name,
              "score": round(_FTS_SCORE_DECAY / (1.0 + i), 3),
              "stage": "fts", "source_tool": "bm25"})
    for cid, clabel in cjk_rows:        # ③ CJK 子串（最低置信）
        _add({"id": cid, "label": clabel, "score": _CJK_SCORE,
              "stage": "cjk", "source_tool": "substring"})
    shape["scanned"] = len(seen)        # N1 scanned = 候选池大小（去重后）

    # token 预算装配：结果子集贪心截断（计数方式与测试断言同源：无 tiktoken 时 len//4）
    mode = _count_tokens("")[1]         # 惰性探测一次并固定 count_mode
    shape["count_mode"] = mode
    truncated, used = [], 0
    for item in results:
        n, _ = _count_tokens(json.dumps(item, ensure_ascii=False))
        if used + n > token_budget:
            break
        used += n
        truncated.append(item)
    results = truncated

    # gap 联动：identifier token 与 gap.ref 精确匹配（大小写不敏感）；模糊匹配明确不做
    gap_hit = False
    gap_summary = []
    for tok in identifiers:
        entry = gap_refs.get(tok.lower())
        if entry:
            gap_hit = True
            gap_summary.append(entry)
    shape["gap_summary"] = gap_summary

    return {"results": results, "query_shape": shape, "gap_hit": gap_hit}


def format_ranked(r: dict) -> str:
    """序列化为 MCP 响应体（方案 §4-B1：B1 是 JSON 工具，响应体 _meta 对象携带工具侧
    诚实自报 query_shape/gap_hit；serve 尾部 `_meta:` 信封行另管 verdict/freshness）。"""
    body = {
        "results": r["results"],
        "_meta": {
            "query_shape": r["query_shape"],
            "gap_hit": r["gap_hit"],
        },
    }
    return json.dumps(body, ensure_ascii=False)
