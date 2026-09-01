"""B3：dispatch 双判据 / 有向视图反向闭包（R3-1）/ fan-out label 匹配（R3-2）/ 生产形态 fixture."""
import json, sqlite3, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import networkx as nx
import pytest
from adapter import _dispatch_candidate, _symbol_short_name


def _mk_db(tmp_path, nodes, edges):
    """构造 B3 分发测试 DB：nodes=(id,kind,name,qname,file_path,lang,sl,el),
    edges=(source,target,kind,metadata). 返回 db 路径。"""
    db = tmp_path / ".codegraph" / "codegraph.db"
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY, kind TEXT, name TEXT, "
                 "qualified_name TEXT, file_path TEXT, language TEXT, start_line INT, "
                 "end_line INT, docstring TEXT, signature TEXT)")
    conn.execute("CREATE TABLE edges (id INTEGER PRIMARY KEY, source TEXT, target TEXT, "
                 "kind TEXT, metadata TEXT, line INT, col INT, provenance TEXT)")
    conn.executemany(
        "INSERT INTO nodes VALUES (?,?,?,?,?,?,?,?,?,?)",
        [n + (None, None) for n in nodes])
    conn.executemany("INSERT INTO edges (source,target,kind,metadata) VALUES (?,?,?,?)", edges)
    conn.commit()
    conn.close()
    return db

def test_dispatch_candidate_double_criteria():
    assert _dispatch_candidate({"resolvedBy": "exact-match", "confidence": 0.99}) is False
    assert _dispatch_candidate({"resolvedBy": "instance-method", "confidence": 0.99}) is True
    assert _dispatch_candidate({"resolvedBy": "exact-match", "confidence": 0.4}) is True
    assert _dispatch_candidate(None) is True   # 无 metadata = resolvedBy None 分支（宁多标勿漏标）

def test_symbol_short_name_from_label():
    """R3-2：合并图节点 id 是 hash 形态且无 name 属性——短名只能来自 label 末段."""
    assert _symbol_short_name({"label": "pkg.Base.handle"}, "function:52e9d06d") == "handle"
    assert _symbol_short_name({"label": "main (2)"}, "function:abc") == "main"   # 消歧后缀剥离

def test_edge_dispatch_hinge_by_file_and_short_names(tmp_path):
    """R4-2 铰链改键：合并边无 DB edge id——source_file+kind+两端短名 JOIN 定组，
    组内最保守取值（Q11 落地）."""
    import json as _json, sqlite3 as _sq
    from adapter import _edge_dispatch_info
    db = tmp_path / ".codegraph" / "codegraph.db"; db.parent.mkdir(parents=True)
    conn = _sq.connect(db)
    conn.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY, kind TEXT, name TEXT, "
                 "qualified_name TEXT, file_path TEXT, language TEXT, start_line INT, "
                 "end_line INT, docstring TEXT, signature TEXT)")
    conn.execute("CREATE TABLE edges (id INTEGER PRIMARY KEY, source TEXT, target TEXT, "
                 "kind TEXT, metadata TEXT, line INT, col INT, provenance TEXT)")
    conn.executemany("INSERT INTO nodes VALUES (?,?,?,?,?,?,?,?,?,?)", [
        ("function:aa", "function", "caller", "pkg.caller", "pkg/caller.py", "python", 1, 9, None, None),
        ("function:bb", "function", "handle", "pkg.Base.handle", "pkg/base.py", "python", 1, 9, None, None)])
    conn.executemany("INSERT INTO edges (source,target,kind,metadata) VALUES (?,?,?,?)", [
        ("function:aa", "function:bb", "calls", _json.dumps({"resolvedBy": "exact-match", "confidence": 0.99})),
        ("function:aa", "function:bb", "calls", _json.dumps({"resolvedBy": "fuzzy", "confidence": 0.4}))])
    conn.commit()
    info = _edge_dispatch_info(conn, "pkg/caller.py", "calls", "caller", "handle")
    conn.close()
    assert info["confidence"] == 0.4                       # 组内最保守（Q11：不洗白猜测边）
    assert info["dispatch_candidate"] is True              # 任一命中即 true
    assert set(info["resolvedBy"]) == {"exact-match", "fuzzy"}   # resolvedBy 数组展示全部

def _fanout_digraph():
    """G2+N4+R3-2：kind 齐全（owner 判定按 kind=='class'）+ label 承载符号名 +
    hash 形态 id——贴近生产 adapter 产物，不给 '.py 后缀'/'id 切段' 启发式留空子."""
    DG = nx.DiGraph()
    DG.add_node("function:c11", kind="function", label="pkg.caller")
    DG.add_node("class:b01", kind="class", label="pkg.Base")
    DG.add_node("function:f01", kind="function", label="pkg.Base.handle")
    DG.add_node("class:a01", kind="class", label="pkg.ChildA")
    DG.add_node("function:f02", kind="function", label="pkg.ChildA.handle")
    DG.add_node("class:a02", kind="class", label="pkg.ChildB")
    DG.add_node("function:f03", kind="function", label="pkg.ChildB.handle")
    DG.add_edge("function:c11", "function:f01", relation="calls")
    DG.add_edge("class:b01", "function:f01", relation="contains")   # class -> method（实测 384 条形态）
    DG.add_edge("class:a01", "function:f02", relation="contains")
    DG.add_edge("class:a02", "function:f03", relation="contains")
    DG.add_edge("class:a01", "class:b01", relation="extends")
    DG.add_edge("class:a02", "class:b01", relation="extends")
    return DG

def test_fanout_expands_all_subclass_overrides():
    from graphify.serve import _fanout_targets
    targets, note = _fanout_targets(_fanout_digraph(), "function:f01")
    assert set(targets) == {"function:f01", "function:f02", "function:f03"}   # 短名 handle 同名匹配
    assert note == ""     # 全部子类 override 覆盖，无降级

def test_fanout_no_owning_class_degrades():
    """method 直接被 file contains（fork 实测 16 条形态）-> 降级返回原单目标.
    N4：owner 节点 kind='file'（非 class）——实现不得用文件后缀启发式."""
    from graphify.serve import _fanout_targets
    DG = nx.DiGraph()
    DG.add_node("file:ff1", kind="file", label="pkg.caller.py")
    DG.add_node("function:lf1", kind="function", label="pkg.loose_fn")
    DG.add_edge("file:ff1", "function:lf1", relation="calls")
    DG.add_edge("file:ff1", "function:lf1", relation="contains")   # owner 是 file 非 class
    targets, note = _fanout_targets(DG, "function:lf1")
    assert targets == ["function:lf1"] and "no owning class" in note

def test_blast_radius_depth2_directed():
    """纯 DiGraph 语义用例：反向闭包不含下游（与 R3-1 端到端用例互补）."""
    from graphify.serve import _reverse_closure
    DG = nx.DiGraph()
    DG.add_edges_from([("app", "mid"), ("mid", "leaf"), ("leaf", "util")])
    ids, total = _reverse_closure(DG, "leaf", depth=2, top_k=50)
    assert set(ids) == {"mid", "app"} and total == 2   # 下游 util 不混入

def test_reverse_closure_from_undirected_graph_json(tmp_path):
    """R3-1 端到端：生产 graph.json 实测 directed:false（serve._load_graph 产 nx.Graph）——
    _digraph_view 从原始 links 重建方向；无向图上'反向闭包'会混入下游 util（系统性过报）."""
    from graphify.serve import _digraph_view, _reverse_closure
    g = tmp_path / "graph.json"
    g.write_text(json.dumps({
        "directed": False, "multigraph": False, "graph": {},
        "nodes": [{"id": "app"}, {"id": "mid"}, {"id": "leaf"}, {"id": "util"}],
        "links": [{"source": "app", "target": "mid"}, {"source": "mid", "target": "leaf"},
                  {"source": "leaf", "target": "util"}]}), encoding="utf-8")
    ids, total = _reverse_closure(_digraph_view(g), "leaf", depth=2, top_k=50)
    assert set(ids) == {"mid", "app"} and "util" not in ids and total == 2


# === 以下为 B3 补充锁定（controller 交接：TypeError 防误用 / 生产形态属性 / 5 元组信封 / 缺省逐字节）====

def test_reverse_closure_on_reverse_view_is_forward_closure():
    """B3 out 方向：_reverse_closure(DG.reverse(), node) = 正向闭包（descendants）——
    函数语义是"输入图里的祖先"，输入是反向视图时 = 原图后代；双 reverse 是显式契约，
    锁定防重构破坏（blast_radius_lines 的 direction=out 依赖它）."""
    from graphify.serve import _reverse_closure
    DG = nx.DiGraph()
    DG.add_edges_from([("app", "mid"), ("mid", "leaf"), ("leaf", "util")])
    ids, total = _reverse_closure(DG.reverse(copy=False), "mid", depth=2, top_k=50)
    assert set(ids) == {"leaf", "util"} and total == 2   # 上游 app 不混入

def test_blast_radius_lines_in_out_both(tmp_path):
    """B3 端到端：_blast_radius_lines 在有向夹具上 in=祖先 / out=后代 / both=并集；
    db_path=None（无 DB）时跳过 dispatch 标注（depth=1 等价的不查 DB 路径）."""
    from graphify.serve import _blast_radius_lines
    DG = _fanout_digraph()
    # f01 的祖先：c11（calls）+ b01（contains）；子类 ChildA/ChildB 非祖先不混入
    lines, total = _blast_radius_lines(DG, "function:f01", "in", 1, 50, "", db_path=None)
    body = "\n".join(lines)
    assert total == 2 and "pkg.caller" in body and "pkg.Base" in body
    assert "ChildA" not in body and "ChildB" not in body
    # out：b01 的后代 = f01（contains）
    lines, total = _blast_radius_lines(DG, "class:b01", "out", 1, 50, "", db_path=None)
    assert total == 1 and "pkg.Base.handle" in "\n".join(lines)
    # both：f01 的并集 = 祖先 {c11, b01}（无后代，反向/正向集同）
    lines, total = _blast_radius_lines(DG, "function:f01", "both", 1, 50, "", db_path=None)
    assert total == 2

def test_reverse_closure_rejects_undirected_graph():
    """B3 硬化：_reverse_closure 只吃 DiGraph——无向图传入直接 TypeError（类型注解强制
    不足以防运行期误用，nx.Graph 也是 nx.Graph 子类，须运行时 isinstance 断言）."""
    from graphify.serve import _reverse_closure
    G = nx.Graph()
    G.add_edges_from([("a", "b"), ("b", "c")])
    try:
        _reverse_closure(G, "b", depth=2, top_k=50)
    except TypeError:
        return
    raise AssertionError("_reverse_closure 必须拒绝无向图（nx.Graph）")

def test_digraph_view_carries_kind_and_label_attrs(tmp_path):
    """E2/N4 fixture 纪律：_digraph_view 产物节点必须带 kind/label 属性（fan-out owner
    判定按 kind=='class'、同名匹配按 label）——生产 adapter 产物的既有契约."""
    from graphify.serve import _digraph_view
    g = tmp_path / "graph.json"
    g.write_text(json.dumps({
        "nodes": [{"id": "function:f01", "kind": "function", "label": "pkg.Base.handle"},
                  {"id": "class:b01", "kind": "class", "label": "pkg.Base"}],
        "links": [{"source": "class:b01", "target": "function:f01", "relation": "contains",
                   "source_file": "pkg/base.py", "source_location": "L5"}],
        "directed": False}), encoding="utf-8")
    DG = _digraph_view(g)
    assert DG.nodes["function:f01"]["kind"] == "function"
    assert DG.nodes["class:b01"]["label"] == "pkg.Base"
    assert DG.edges["class:b01", "function:f01"]["relation"] == "contains"
    assert DG.edges["class:b01", "function:f01"]["source_file"] == "pkg/base.py"

def test_apply_envelope_five_tuple_carries_closure_size():
    """B3 N1 扩展：5 元组 (text, found, scanned, override, extra_meta) 时 extra_meta 并入
    _meta（blast-radius 的 _meta.closure_size）；3/4 元组路径不受影响（回归锚点在
    test_response_envelope.py）."""
    from graphify.serve import _apply_envelope
    out = _apply_envelope("get_neighbors", ("Neighbors of x:", True, 7, "low_confidence",
                                            {"closure_size": 7}), freshness="fresh")
    meta = json.loads(out.rstrip("\n").split("\n")[-1].removeprefix("_meta: "))
    assert meta["verdict"] == "low_confidence" and meta["closure_size"] == 7

def test_neighbors_legacy_lines_byte_identical():
    """缺省路径回归锁定：direction/depth 未给定时 successors/predecessors 循环输出
    与扩展前逐字节一致（无向图上双向输出本就同集合；方向只在 B3 有向视图路径用）."""
    from graphify.serve import _neighbors_lines
    G = nx.Graph()
    G.add_node("n1", label="caller", source_file="pkg/caller.py", source_location="L10")
    G.add_node("n2", label="callee", source_file="pkg/callee.py", source_location="L5")
    # 生产形态：source_file/source_location 在边上（关系 SITE = 调用点所在源文件，
    # 非 def 行，#BUG1）——两条方向线展示同一条边的 at=。
    G.add_edge("n1", "n2", relation="calls", confidence="EXTRACTED",
               source_file="pkg/caller.py", source_location="L10")
    lines = _neighbors_lines(G, "n1")
    assert lines[0] == "Neighbors of caller:"
    assert "  --> callee [calls] [EXTRACTED] at=pkg/caller.py:L10" in lines
    assert "  <-- callee [calls] [EXTRACTED] at=pkg/caller.py:L10" in lines  # 无向图双向同集合
    # 字节锚点：完整 join 与当前实现逐字符一致
    expected = "\n".join([
        "Neighbors of caller:",
        "  --> callee [calls] [EXTRACTED] at=pkg/caller.py:L10",
        "  <-- callee [calls] [EXTRACTED] at=pkg/caller.py:L10",
    ])
    assert "\n".join(lines) == expected

def test_serve_selfcontained_copies_match_adapter():
    """三处同源防漂移（rebuild_entry _parse_refresh 先例）：serve.py 不 import scripts
    的既有约束——B3 分发三函数在 serve 持自包含副本，语义须与 adapter 本体逐样本一致."""
    from graphify.serve import _dispatch_candidate as s_dc, _symbol_short_name as s_ssn
    samples = [
        {"resolvedBy": "exact-match", "confidence": 0.99},
        {"resolvedBy": "instance-method", "confidence": 0.99},
        {"resolvedBy": "exact-match", "confidence": 0.4},
        {"resolvedBy": "qualified-name", "confidence": 0.95},
        {"resolvedBy": "fuzzy"},
        {"valueRef": True},
        None,
    ]
    for m in samples:
        assert s_dc(m) == _dispatch_candidate(m), f"dispatch_candidate 漂移: {m!r}"
    for d, nid in [({"label": "pkg.Base.handle"}, "function:52e9d06d"),
                   ({"label": "main (2)"}, "function:abc"),
                   ({}, "function:xyz")]:
        assert s_ssn(d, nid) == _symbol_short_name(d, nid), f"symbol_short_name 漂移: {d!r} {nid!r}"


# === 以下为 review C1/I1 + 补充审核 M1a/M1b/M5 的修复锁定（计划所有者确认的必带测试）====

def test_blast_radius_out_dispatch_hit_not_fallback(tmp_path):
    """C1 命中路径断言（关键）：out 方向 fixture + 真 DB 行（resolvedBy=instance-method）
    → 断言输出含 resolvedBy=instance-method 而非 unknown——区分命中/回退态（out src/tgt
    互换时 JOIN 落空组→unknown，此测防回退态吞失效信号；结构性根因）。"""
    from graphify.serve import _blast_radius_lines
    db = _mk_db(tmp_path, [
        ("function:cc", "function", "caller", "pkg.caller", "pkg/caller.py", "python", 1, 9),
        ("method:bb", "method", "handle", "pkg.Base.handle", "pkg/base.py", "python", 1, 9)],
        [("function:cc", "method:bb", "calls",
          json.dumps({"resolvedBy": "instance-method", "confidence": 0.7}))])
    DG = nx.DiGraph()
    DG.add_node("function:c1", kind="function", label="pkg.caller")
    DG.add_node("method:b1", kind="method", label="pkg.Base.handle")
    DG.add_edge("function:c1", "method:b1", relation="calls", source_file="pkg/caller.py",
                confidence="EXTRACTED")
    # out/depth=2（R5-5(b)：dispatch 仅 depth>1 路径）：闭包 = callees {b1}，toward-edge (c1->b1)
    lines, total = _blast_radius_lines(DG, "function:c1", "out", 2, 50, "", db_path=db)
    body = "\n".join(lines)
    assert "resolvedBy=instance-method" in body, body
    assert "unknown" not in body   # 命中态不是回退态

def test_blast_radius_out_fanout_lists_subclass_overrides(tmp_path):
    """C1 out 方向 fanout：caller 调 Base.handle（instance-method 候选），Base 有 Child
    覆写——从 caller 向 out 走，fanout 应列被调方 Base.handle 的子类覆写 Child.handle
    （当前 bug 会 fanout 调用侧 caller → 'no owning class' 降级，非覆写列表）。"""
    from graphify.serve import _blast_radius_lines
    db = _mk_db(tmp_path, [
        ("function:cc", "function", "caller", "pkg.caller", "pkg/caller.py", "python", 1, 9),
        ("method:bb", "method", "handle", "pkg.Base.handle", "pkg/base.py", "python", 1, 9)],
        [("function:cc", "method:bb", "calls",
          json.dumps({"resolvedBy": "instance-method", "confidence": 0.7}))])
    DG = nx.DiGraph()
    DG.add_node("function:c1", kind="function", label="pkg.caller")
    DG.add_node("class:b01", kind="class", label="pkg.Base")
    DG.add_node("method:b1", kind="method", label="pkg.Base.handle")
    DG.add_node("class:c01", kind="class", label="pkg.Child")
    DG.add_node("method:c2", kind="method", label="pkg.Child.handle")
    DG.add_edge("function:c1", "method:b1", relation="calls", source_file="pkg/caller.py",
                confidence="EXTRACTED")
    DG.add_edge("class:b01", "method:b1", relation="contains")
    DG.add_edge("class:c01", "method:c2", relation="contains")
    DG.add_edge("class:c01", "class:b01", relation="extends")
    lines, total = _blast_radius_lines(DG, "function:c1", "out", 2, 50, "", db_path=db)
    body = "\n".join(lines)
    assert "Child.handle" in body, body   # fanout 展开被调方类层级，非调用侧

def test_blast_radius_contains_edge_no_dispatch_annotation(tmp_path):
    """I1：contains 边在闭包内无 dispatch 标注——非 calls 边不查 DB、不标 dispatch、
    不 fanout（god node 噪声根除：52 行中 44 行）。DB 存在且含 contains 行也不查询。"""
    from graphify.serve import _blast_radius_lines
    db = _mk_db(tmp_path, [
        ("class:bb", "class", "Base", "pkg.Base", "pkg/base.py", "python", 1, 9),
        ("method:bh", "method", "handle", "pkg.Base.handle", "pkg/base.py", "python", 1, 9)],
        [("class:bb", "method:bh", "contains",
          json.dumps({"resolvedBy": "exact-match", "confidence": 0.99}))])
    DG = nx.DiGraph()
    DG.add_node("class:b01", kind="class", label="pkg.Base")
    DG.add_node("method:b1", kind="method", label="pkg.Base.handle")
    DG.add_edge("class:b01", "method:b1", relation="contains", source_file="pkg/base.py",
                confidence="EXTRACTED")
    lines, total = _blast_radius_lines(DG, "class:b01", "out", 2, 50, "", db_path=db)
    body = "\n".join(lines)
    assert "resolvedBy" not in body and "dispatch" not in body and "fanout" not in body, body

def test_blast_radius_fanout_limited_by_edge_kinds(tmp_path):
    """M1b：edge_kinds=['calls'] 滤掉 contains/extends → fanout 展开受限，降级 note 用
    'limited by edge_kinds filter' 而非 'no owning class'（区分"被过滤"与"无所属类"）。"""
    from graphify.serve import _blast_radius_lines
    db = _mk_db(tmp_path, [
        ("function:cc", "function", "caller", "pkg.caller", "pkg/caller.py", "python", 1, 9),
        ("method:bb", "method", "handle", "pkg.Base.handle", "pkg/base.py", "python", 1, 9)],
        [("function:cc", "method:bb", "calls",
          json.dumps({"resolvedBy": "instance-method", "confidence": 0.7}))])
    DG = nx.DiGraph()
    DG.add_node("function:c1", kind="function", label="pkg.caller")
    DG.add_node("class:b01", kind="class", label="pkg.Base")
    DG.add_node("method:b1", kind="method", label="pkg.Base.handle")
    DG.add_node("class:c01", kind="class", label="pkg.Child")
    DG.add_node("method:c2", kind="method", label="pkg.Child.handle")
    DG.add_edge("function:c1", "method:b1", relation="calls", source_file="pkg/caller.py",
                confidence="EXTRACTED")
    DG.add_edge("class:b01", "method:b1", relation="contains")
    DG.add_edge("class:c01", "method:c2", relation="contains")
    DG.add_edge("class:c01", "class:b01", relation="extends")
    # 过滤后只剩 calls（contains/extends 缺席）→ fanout_limited=True
    DGF = nx.DiGraph()
    DGF.add_nodes_from(DG.nodes(data=True))
    for u, v, d in DG.edges(data=True):
        if str(d.get("relation", "")).lower() == "calls":
            DGF.add_edge(u, v, **d)
    lines, _ = _blast_radius_lines(DGF, "function:c1", "out", 2, 50, "",
                                   db_path=db, fanout_limited=True)
    body = "\n".join(lines)
    assert "limited by edge_kinds filter" in body, body
    assert "no owning class" not in body
    # 完整图（无过滤）→ fanout 正常展开 Child.handle
    lines2, _ = _blast_radius_lines(DG, "function:c1", "out", 2, 50, "", db_path=db)
    assert "Child.handle" in "\n".join(lines2)

def test_blast_radius_both_truncation_labels_both_directions():
    """M1a：both 方向截断行标注 by degree across both directions（in/out 并集语义
    对称性透明化）；in 方向仍用 by degree（不带 both 标注）。"""
    from graphify.serve import _blast_radius_lines
    DG = _fanout_digraph()
    lines, total = _blast_radius_lines(DG, "function:f01", "both", 2, 1, "", db_path=None)
    assert total > 1
    assert any("by degree across both directions" in ln for ln in lines)
    lines, total = _blast_radius_lines(DG, "function:f01", "in", 2, 1, "", db_path=None)
    assert any("by degree" in ln and "both directions" not in ln for ln in lines)

def test_fanout_targets_rejects_undirected_graph():
    """M5：_fanout_targets 与 _reverse_closure 一致拒无向图（TypeError，运行时断言）."""
    from graphify.serve import _fanout_targets
    G = nx.Graph()
    G.add_edge("a", "b")
    try:
        _fanout_targets(G, "a")
    except TypeError:
        return
    raise AssertionError("_fanout_targets 必须拒绝无向图（nx.Graph）")

@pytest.mark.golden
def test_join_hit_rate_real_db_golden():
    """生产探针固化（计划所有者建议）：正确顺序 JOIN（source_file+kind=calls+src短名+
    tgt短名）在 fork 真实 DB 上的命中率 ≥ 阈值——防"回退态吞失效信号"回归（out src/tgt
    互换或短名映射退化时命中率崩）。金标门（conftest）：真实 DB 缺失自动跳过。"""
    from graphify.serve import _digraph_view, _edge_dispatch_info, _symbol_short_name
    root = Path(__import__("os").environ.get("GRAPHIFY_GOLDEN_ROOT", r"D:/code/graphify_fork"))
    db = root / ".codegraph" / "codegraph.db"
    graph = root / "graphify-out" / "graph.json"
    if not graph.exists():
        pytest.skip(f"skipped (golden): {graph} 缺失")
    DG = _digraph_view(graph)
    hits = misses = 0
    try:
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        try:
            for u, v, ed in DG.edges(data=True):
                if str(ed.get("relation", "")) != "calls":
                    continue
                source_file = ed.get("source_file") or DG.nodes[u].get("source_file") or ""
                src_short = _symbol_short_name(DG.nodes[u], u)
                tgt_short = _symbol_short_name(DG.nodes[v], v)
                if _edge_dispatch_info(conn, source_file, "calls", src_short, tgt_short)["resolvedBy"]:
                    hits += 1
                else:
                    misses += 1
        finally:
            conn.close()
    except sqlite3.Error:
        pytest.skip(f"skipped (golden): {db} 不可读")
    total = hits + misses
    if total == 0:
        pytest.skip("skipped (golden): 无 calls 边样本")
    rate = hits / total
    # 阈值 60%：基线 84.6%（11764 calls 边，2026-09-01 实测）——余量 25pt 防抖动，
    # 仍能抓住灾难性回归（src/tgt 互换或短名映射退化时命中率崩到近 0）。
    assert rate >= 0.6, (f"正确顺序 JOIN 命中率 {rate:.0%}（{hits}/{total}）< 60%——"
                         f"out src/tgt 互换或短名映射退化（基线 84.6%，见 task-9-report）")
