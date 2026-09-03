"""B1 融合检索：FTS5 × 精确名 pinning × 结构 × 预算 四通道（只读直查自建 FTS 缓存 + 事实层 graph.json）。

分层原理（docs/graphify-native-indexing-spec.md §工具面迁移）：07 票换源——词法检索
（FTS5 BM25 / 精确名 pinning）查 `graphify-out/.fts-index.db`（自建缓存，05 票 schema/
权重/tokenizer 逐字平移 codegraph，排序等价）；结构（度数中心性）与 gap 通道走
`graphify-out/graph.json` 事实层（graph.json 是唯一事实来源，缓存由它投影）。
token 级分流（Q8）：identifier tokens 进 FTS/pinning，CJK tokens 进图侧子串
（R18：纯中文 FTS 必零命中，显式报告 cjk_hits 杜绝"没搜到=不存在"）。

降级（M4 诚实 absent 保留）：graph.json 缺失/损坏（事实层没了）→ absent + db_missing
（旧链路"非 codegraph 项目"同构）；缓存不可用（scripts 缺失/构建失败）但事实层在 →
纯图通道（structure/cjk/gap）照常，fts/pinned 诚实零命中 + cache_missing 标注。

返回 dict：{"results": [{"id","label","score","stage","source_tool"}...],
           "query_shape": {...}, "gap_hit": bool}。serve 闭包用 format_ranked 序列化为
JSON 响应体（B1 是 JSON 工具），再经 N1 信封（serve.py `_apply_envelope`）装配
verdict/freshness。工具侧诚实自报（query_shape/count_mode/collisions/gap_hit）经
format_ranked 归入响应体 `_meta` 对象（方案 §4-B1：`_meta.query_shape` 报告）。
"""
from __future__ import annotations
import json, re, sqlite3, sys, time
from pathlib import Path

import networkx as nx  # B3 I2：get_digraph 统一缓存的 DiGraph 视图（graphify 核心依赖）

_SELF = Path(__file__).resolve().parent
if str(_SELF) not in sys.path:        # scripts 无包结构——同目录 import（rebuild_entry 先例）
    sys.path.insert(0, str(_SELF))

# 07 票：codegraph 运行时退役——B1 不再直查 .codegraph/codegraph.db（adapter 依赖移除），
# 改查自建 FTS 缓存（graphify-out/.fts-index.db，scripts/fts_cache.py，05 票产物）。
# fts_cache 是 scripts 模块，lazy import（见 _fts_cache）——graphify 包不反向依赖 scripts/。
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

# M1/I2：单入口单一载荷缓存 {graph_path_str: (mtime_ns, size, payload_dict)}。一次
# json.loads 产出 B1 结构派生数据（degree/collision_bases/nodes）+ B3 有向视图（DiGraph，
# lazy——首次 get_digraph 请求时从已缓存 links 构造并驻留同一 payload，见 get_digraph）。
# 失效语义：mtime 与 size 双键命中才复用——rebuild 后 mtime 必变自动失效（Windows
# st_mtime_ns 精度足够，~100ns 粒度远小于 rebuild 间隔）。缓存按 path_str 分键，多项目
# 热切换互不串。并发安全：FastMCP sync handler 实际跑线程池，并发请求存在竞争——但
# CPython dict 单键赋值原子（GIL），并发 miss 时重复加载无害（payload 幂等，后写覆盖
# 先写内容相同），无需加锁；禁止以"单协程顺序执行"假设为依据添加非原子操作（如分步
# get-then-put 删改，会在并发下撕裂）。M-A（补充审核）：get_digraph 的 lazy 构造同此
# 合法——check-then-act（digraph is None 检查与赋值分离）下并发双建内容相同（同一只读
# payload 构建），一次性赋值幂等覆盖无害，与缓存主体同一论证；构建后 payload 视为
# 不可变。L-A（补充审核）：digraph 构建完成后 payload["links"]=None 释放边列表（边数据
# 已全部并入 digraph），B1-only 会话不驻留整图边列表。L-B 容量上界：
# _GRAPH_CACHE_MAX_ENTRIES = 8，多项目热切换下最多驻留 8 份解析后整图
# （~50-150MB/项目），防无限增长；DiGraph 视图驻留同 entry 不另计。
_GRAPH_CACHE_MAX_ENTRIES = 8
_GRAPH_CACHE: dict[str, tuple[int, int, dict]] = {}
_graph_loads = 0  # M1 诊断：实际 json.loads 全量图次数（cache miss 才 +1），供测试/日志验证命中率


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


def _fts_cache():
    """lazy fts_cache（scripts/ 无包结构——graphify 包不反向依赖 scripts/，serve 同款
    _fts_cache 先例：消费侧调用时才挂 sys.path）。import 失败（非 editable 安装缺
    scripts/）→ None——调用方诚实降级（fts/pinned 通道零命中 + cache_missing 标注），
    不逃逸 ModuleNotFoundError。"""
    try:
        import fts_cache
        return fts_cache
    except ImportError:
        return None


def _ensure_fts_retry(graph_path, fts_path, attempts=5, base_delay=0.1):
    """ensure_fts + os.replace 锁重试退避（serve._ensure_fts_retry 同款逻辑，ranked 自持
    一份——serve 不反向依赖 scripts，ranked 亦不 import serve，避免循环）。指纹命中/重建
    成功返回 True；重试耗尽向上抛（缓存构建失败是事实层派生失败，ranked_context 的 except
    捕获后走纯图通道降级）。fts_cache 不可用（_fts_cache 返回 None）→ False 不重建。"""
    fts = _fts_cache()
    if fts is None:
        return False
    delay = base_delay
    for attempt in range(attempts):
        try:
            return fts.ensure_fts(graph_path, fts_path)
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay)
            delay *= 2
    return False


def _fts_search(conn, identifiers: list[str]):
    """FTS5 BM25 通道（07 票换源）：查自建 FTS 缓存 nodes_fts（05 schema 逐字平移
    codegraph——同 5 列、同列权重 id=0/name=3/qn=2/doc=0.2/sig=1、同 unicode61 tokenizer，
    排序等价），叠加 camel 双侧预拆（split_identifier 索引与查询共用，pinningSearch ↔
    "pinning search" 语义命中）。委托 fts_cache.fts_search（同 snippet 列 3、同 MATCH
    隐式 AND、同 LIMIT 40）。返回 (rows, hits)，rows = (id, name, qualified_name, snippet,
    bm25)；缓存不可用/语法异常 → 诚实零命中。"""
    fts = _fts_cache()
    if fts is None or not identifiers:
        return [], 0
    return fts.fts_search(conn, identifiers)


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


def _gap_refs(failed_refs):
    """Knowledge Gaps（07 票换源）：extract 失败收集器（Task 04，{from_node, callee_name,
    line, file_path}）→ {ref.lower(): {ref, files[top3]}}——替代 codegraph unresolved_refs
    status='failed'。匹配语义不变（查询词与失败引用名大小写不敏感精确匹配）。缺失
    （旧图无 failed_refs）→ 诚实空（R18：没搜到 ≠ 不存在）。"""
    out = {}
    for fr in failed_refs or []:
        if not isinstance(fr, dict):
            continue
        ref = fr.get("callee_name") or ""
        if not ref:
            continue
        key = ref.lower()
        entry = out.setdefault(key, {"ref": ref, "files": []})
        file_path = fr.get("file_path") or ""
        line = fr.get("line") or 0
        if file_path and len(entry["files"]) < _GAP_FILES_TOP:
            entry["files"].append(f"{file_path}:{line}" if line else file_path)
    return out


def _cache_load(graph_path: Path) -> dict | None:
    """统一缓存核心（I2 单入口）：mtime+size 双键命中复用；miss 则一次 json.loads 解析
    graph.json → 派生 degree/collision_bases/nodes/links（B3 DiGraph 视图 lazy 后置，
    get_digraph 构造后 payload["links"] 置 None 释放——L-A，见 get_digraph）。
    失败（缺失/损坏/越界）→ None，不缓存（下次修复即拾起）。"""
    key = str(graph_path)
    try:
        st = graph_path.stat()
    except OSError:
        return None
    hit = _GRAPH_CACHE.get(key)
    if hit is not None and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
        # L-B 命中移到最新：del 后重插（Python 3.7+ dict 保插入序，迭代首键即最旧）
        del _GRAPH_CACHE[key]
        _GRAPH_CACHE[key] = hit
        return hit[2]
    try:
        g = json.loads(graph_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    # 兼容旧图：edges 键等价 links（与 graphify/serve._load_graph 同款 shim）
    if "links" not in g and "edges" in g:
        g = dict(g, links=g["edges"])
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
    # 07 票：failed_refs（知识缺口查询源）随 graph.json 载荷进缓存 —— gap 通道消费。
    # 顶层为规范落点（--no-cluster 直写与 to_json 提升路径同源）；graph 属性槽回退
    # 兜底（第三方写入的嵌套形态，防御性读取）。空 → []（诚实空，无缺口）。
    payload = {
        "degree": degree,
        "collision_bases": collision_bases,
        "nodes": g.get("nodes", []),
        "links": g.get("links", []),
        "digraph": None,   # B3 lazy 有向视图（首次 get_digraph 时构造驻留）
        "failed_refs": g.get("failed_refs") or (g.get("graph") or {}).get("failed_refs") or [],
    }
    # 共享可变引用：payload（nodes/links list、degree dict、digraph）与调用方共享同一
    # 对象，调用方须只读；mutate 会污染缓存（后续命中直接复用该对象）。并发 miss 重复
    # 解析无害（幂等，后写覆盖先写内容相同）。
    global _graph_loads
    _graph_loads += 1
    _GRAPH_CACHE[key] = (st.st_mtime_ns, st.st_size, payload)
    if len(_GRAPH_CACHE) > _GRAPH_CACHE_MAX_ENTRIES:
        oldest = next(iter(_GRAPH_CACHE))   # 插入序首键 = 最旧条目（LRU 驱逐）
        del _GRAPH_CACHE[oldest]
    return payload


def get_digraph(graph_path: Path):
    """B3 有向视图（R3-1 从 links source/target 重建方向）：I2 统一缓存的 lazy 双视图——
    B1 payload 已解析一次，DiGraph 首次请求时从已缓存 links 构造并驻留同一 entry（不二次
    json.loads）。生产 graph.json 实测 directed:false，node_link_graph 无向化丢方向，须从
    原始 links 重建。节点/边属性（kind/label/source_file/relation/confidence）原样带上
    （E2/N4 fixture 纪律）。调用方（serve._digraph_view）只读，禁止 mutate。失败 → 空
    DiGraph（诚实降级，不崩出口）。构造完成后 payload["links"] 置 None 释放（L-A：边数据
    已全部并入 digraph，B1-only 会话不驻留整图边列表）。"""
    payload = _cache_load(graph_path)
    if payload is None:
        return nx.DiGraph()
    if payload["digraph"] is None:
        DG = nx.DiGraph()
        for n in payload["nodes"]:
            nid = n.get("id")
            if nid is None:
                continue
            attrs = {k: v for k, v in n.items() if k != "id"}
            if attrs:
                DG.add_node(nid, **attrs)
            else:
                DG.add_node(nid)
        for lk in payload["links"]:
            s, t = lk.get("source"), lk.get("target")
            if s is None or t is None:
                continue
            DG.add_edge(s, t, **{k: v for k, v in lk.items() if k not in ("source", "target")})
        # M-A（补充审核）：DG 构建完一次性赋值——并发下两线程可都见 digraph is None 各建
        # 一份（check-then-act），但内容相同（同一只读 payload 构建），后写覆盖无害——与
        # 缓存主体同一幂等论证，无需加锁；构建后 payload 视为不可变（调用方只读）。
        payload["digraph"] = DG
        # L-A（补充审核）：links 已全部并入 digraph（边数据/属性原样携带），释放边列表
        # ——B1-only 会话（从不调 get_digraph）不驻留整图边列表 ~20-40MB/项目。grep 核实
        # payload 内无其他 links 消费者（仅本构造期读取），释放安全。
        payload["links"] = None
    return payload["digraph"]


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


def ranked_context(root, query: str, token_budget: int = 2000, channels=None) -> dict:
    """四通道融合检索（07 票换源：codegraph DB → 自建 FTS 缓存 + 事实层 graph.json）。

    root: 项目根（含 graphify-out/graph.json 事实层 + graphify-out/.fts-index.db 缓存）。
    channels: 启用通道子集（None=四通道全开）；"BM25-only" 对照（金标集）传
    {"fts"}——通道关闭时结构信号不参与（BM25-only 口径无度数 tiebreak）。

    降级（M4 诚实 absent 保留，spec 工具面迁移）：graph.json 缺失/损坏（事实层没了）
    → absent + db_missing 标注（旧链路"非 codegraph 项目"同构，零 serve 改动——N1 信封
    found=False + scanned=0 即 absent）；缓存不可用（scripts 缺失 / ensure 构建失败 /
    打开失败）但事实层在 → 纯图通道（structure/cjk/gap）照常，fts/pinned 诚实零命中 +
    cache_missing 标注（"回退纯图通道"行为不变）。
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

    graph_path = root / "graphify-out" / "graph.json"
    # 事实层（graph.json）是唯一事实来源（spec 单一事实层）：结构/度数/cjk/gap 都从它取。
    # 缺失/损坏（_cache_load 返回 None）→ M4 诚实 absent（旧链路"无 codegraph DB"同构——
    # 事实层没了检索无源可查）。旧链路的 raw_db 降级随之退役：graph.json 即事实层，损坏
    # 即整条链不可用，不存在第二个 DB 可回退。
    payload = _cache_load(graph_path)
    if payload is None:
        shape["db_missing"] = True
        shape["centrality"] = "db_missing"
        shape["count_mode"] = _count_tokens("")[1]
        shape["note"] = "graphify-out/graph.json 缺失（无事实层）——非 graphify 项目，B1 检索不可用"
        return {"results": [], "query_shape": shape, "gap_hit": False}
    degree = payload["degree"] if "structure" in active else None
    collision_bases = payload["collision_bases"]
    nodes = payload["nodes"]
    gap_refs = _gap_refs(payload["failed_refs"])

    # FTS 缓存（自建，05 模块）：fts/pinned 通道查 .fts-index.db。缓存不可用（模块缺失/
    # ensure 构建失败/打开失败）→ 诚实降级到纯图通道（structure/cjk/gap 已装载），
    # fts/pinned 零命中 + cache_missing 标注——不逃逸异常。
    fts_rows, fts_hits = [], 0
    pinned_rows, pinned_terms = [], []
    conn = None
    try:
        fts = _fts_cache()
        if fts is None:
            shape["cache_missing"] = True
        else:
            fts_path = root / "graphify-out" / ".fts-index.db"
            _ensure_fts_retry(graph_path, fts_path)
            conn = fts.open_readonly(fts_path)
            if "fts" in active:
                fts_rows, fts_hits = _fts_search(conn, identifiers)
            if "pinned" in active:
                pinned_rows, pinned_terms = _pinning_search(
                    conn, [t for t in identifiers if _is_source_shaped(t)])
    except (sqlite3.Error, OSError, ValueError, RuntimeError):
        shape["cache_missing"] = True
    finally:
        if conn is not None:
            conn.close()
    shape["fts_hits"] = fts_hits
    shape["pinned"] = pinned_terms
    cjk_rows, cjk_hits = (_cjk_search(nodes, cjk) if "cjk" in active else ([], 0))
    shape["cjk_hits"] = cjk_hits

    # 融合装配：stage 优先级（pinned > fts > cjk）+ 通道内排序 + 结构度数平局打破
    # L1 id 恒等不变量：FTS 缓存 id 与 graph.json 图 id 恒等（缓存由 graph.json 投影，
    # 碰撞除外）——degree join 依赖此隐含契约。当前成立：FTS nodes 表 id 与 graph.json
    # 节点 id 逐字同源（05 _build 直通同一 id 池），仅 __cg 消歧后缀折叠（collision_bases
    # 排除）。上游若改 id 生成（加盐/加前缀/改折叠规则），join 会静默退化为 degree 全 0
    # ——以 degenerate_degree 显式标注供诊断，不把"0"误当真实中心度（平局打破静默失效）。
    results, seen = [], set()
    _join_candidates = 0   # 参与 degree join 的候选数（非碰撞）
    _deg_positive = 0      # 其中 degree > 0 的个数（L1 退化判据）

    def _add(item: dict) -> None:
        nonlocal _join_candidates, _deg_positive
        iid = item["id"]
        if iid in seen:
            return
        seen.add(iid)
        d = 0
        if degree is not None:
            if iid in collision_bases:   # 碰撞节点不参与 join（Q6），记录不误当真实度 0
                shape["collisions"] += 1
            else:
                _join_candidates += 1
                d = degree.get(iid, 0)
                if d > 0:
                    _deg_positive += 1
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
    # L1 退化标注：仅 merged_graph 口径（id 恒等不变量相关的 join 路径）——BM25-only
    # （degree=None）不触发。有非碰撞候选但 degree 全 0 时提示 id 恒等不变量疑破；
    # 稀疏图（孤立节点）也可能触发，属诊断信号非错误。
    if degree is not None and shape["centrality"] == "merged_graph" \
            and _join_candidates and _deg_positive == 0:
        shape["degenerate_degree"] = True

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
