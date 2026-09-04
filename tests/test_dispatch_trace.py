"""B3：有向视图反向闭包（R3-1）/ fan-out label 匹配（R3-2）/ 生产形态 fixture.
Task 08：dispatch 概念退役——_dispatch_candidate/_edge_dispatch_info 双副本与金标测试已删，
多态 fanout 判断改读边属性 confidence（INFERRED/AMBIGUOUS 保留信号）+ resolved_by（04 打点），
邻接/影响面工具响应不再含 dispatch_candidate 字段。
"""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import networkx as nx
import pytest
from adapter import _symbol_short_name


def test_symbol_short_name_from_label():
    """R3-2：合并图节点 id 是 hash 形态且无 name 属性——短名只能来自 label 末段."""
    assert _symbol_short_name({"label": "pkg.Base.handle"}, "function:52e9d06d") == "handle"
    assert _symbol_short_name({"label": "main (2)"}, "function:abc") == "main"   # 消歧后缀剥离

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


def _dispatch_digraph():
    """Task 08 新语义夹具：_fanout_digraph + calls 边带提取期原生打点
    confidence/resolved_by（04 产物形态）——fanout 判断读边属性，无 DB。"""
    DG = _fanout_digraph()
    DG.add_edge("function:c11", "function:f01", relation="calls",
                confidence="AMBIGUOUS", resolved_by="instance-method",
                source_file="pkg/caller.py", source_location="L10")
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

def test_blast_radius_lines_in_out_both():
    """B3 端到端：_blast_radius_lines 在有向夹具上 in=祖先 / out=后代 / both=并集；
    depth=1（无多态展开）不标 resolved_by/fanout（R5-5(b) 沿用）。"""
    from graphify.serve import _blast_radius_lines
    DG = _fanout_digraph()
    # f01 的祖先：c11（calls）+ b01（contains）；子类 ChildA/ChildB 非祖先不混入
    lines, total = _blast_radius_lines(DG, "function:f01", "in", 1, 50, "")
    body = "\n".join(lines)
    assert total == 2 and "pkg.caller" in body and "pkg.Base" in body
    assert "ChildA" not in body and "ChildB" not in body
    assert "resolved_by" not in body and "fanout" not in body   # depth=1 不标
    # out：b01 的后代 = f01（contains）
    lines, total = _blast_radius_lines(DG, "class:b01", "out", 1, 50, "")
    assert total == 1 and "pkg.Base.handle" in "\n".join(lines)
    # both：f01 的并集 = 祖先 {c11, b01}（无后代，反向/正向集同）
    lines, total = _blast_radius_lines(DG, "function:f01", "both", 1, 50, "")
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

def test_digraph_view_accepts_str_path(tmp_path):
    """I4（Task 12 实测）：serve 侧 active_graph_path 恒为 str（str(Path(...)) 归一化），
    ranked.get_digraph/_cache_load 走 graph_path.stat() 要求 Path——str 直传会
    AttributeError。_digraph_view 入口统一 Path()（B3 与 C1 两个调用点同受益，
    Task 9 仅以 Path 测试，str 生产路径是未覆盖的集成盲区）。"""
    from graphify.serve import _digraph_view
    g = tmp_path / "graph.json"
    g.write_text(json.dumps({
        "nodes": [{"id": "a", "kind": "function", "label": "a()"}],
        "links": [], "directed": False}), encoding="utf-8")
    DG = _digraph_view(str(g))   # serve 生产形态：str
    assert DG.number_of_nodes() == 1

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

def test_neighbors_lines_exposes_resolved_by_when_present():
    """Task 08：缺省邻接路径也读边属性 resolved_by（04 打点存在时附到行尾）——
    邻接工具响应边置信度与 resolved_by 均可读出（字节锚点 fixture 无该属性，回归不变）。"""
    from graphify.serve import _neighbors_lines
    G = nx.Graph()
    G.add_node("n1", label="caller")
    G.add_node("n2", label="callee")
    G.add_edge("n1", "n2", relation="calls", confidence="INFERRED",
               resolved_by="fuzzy", source_file="pkg/caller.py", source_location="L10")
    lines = _neighbors_lines(G, "n1")
    body = "\n".join(lines)
    assert "  --> callee [calls] [INFERRED] [resolved_by=fuzzy] at=pkg/caller.py:L10" in body
    assert "  <-- callee [calls] [INFERRED] [resolved_by=fuzzy] at=pkg/caller.py:L10" in body

def test_serve_symbol_short_name_matches_adapter():
    """同源自包含防漂移（rebuild_entry _parse_refresh 先例，Task 08 保留）：
    serve.py 不 import scripts 的既有约束——_symbol_short_name 在 serve 持自包含副本，
    语义须与 adapter 本体逐样本一致（fanout 同名匹配依赖它）。"""
    from graphify.serve import _symbol_short_name as s_ssn
    for d, nid in [({"label": "pkg.Base.handle"}, "function:52e9d06d"),
                   ({"label": "main (2)"}, "function:abc"),
                   ({}, "function:xyz")]:
        assert s_ssn(d, nid) == _symbol_short_name(d, nid), f"symbol_short_name 漂移: {d!r} {nid!r}"


# === 以下为 Task 08 新语义锁定（dispatch 退役：边属性原生承载，无 DB）================

def test_blast_radius_response_exposes_confidence_and_resolved_by():
    """Seam 3 工具面：calls 边带提取期打点（confidence=AMBIGUOUS + resolved_by）时，
    blast_radius 响应逐边读出——边置信度与 resolved_by 可从响应中读出（多态 fanout
    判断能力保留）。"""
    from graphify.serve import _blast_radius_lines
    DG = _dispatch_digraph()
    # in/depth=2：f01 的祖先闭包 = {c11, b01}；c11→f01 的 calls 边应标 confidence 与 resolved_by
    lines, total = _blast_radius_lines(DG, "function:f01", "in", 2, 50, "")
    body = "\n".join(lines)
    assert "resolved_by=instance-method" in body, body      # 04 打点可读
    assert "[AMBIGUOUS]" in body, body                      # 边置信度可读（保留信号）
    assert "dispatch_candidate" not in body and "[dispatch" not in body, body  # 旧字段/标注已删

def test_blast_radius_fanout_triggers_on_ambiguous_confidence():
    """Seam 3：INFERRED/AMBIGUOUS = 分发候选——fanout 展开被调方类层级的子类 override
    （caller 调 Base.handle，ChildA/ChildB 覆写）。"""
    from graphify.serve import _blast_radius_lines
    DG = _dispatch_digraph()
    lines, total = _blast_radius_lines(DG, "function:f01", "in", 2, 50, "")
    body = "\n".join(lines)
    assert "pkg.ChildA.handle" in body and "pkg.ChildB.handle" in body, body  # fanout 全量覆写

def test_blast_radius_extracted_edge_not_expanded():
    """Seam 3：EXTRACTED 确定性调用不标注、不展开——即使带 resolved_by（04 打点，
    数据读出仍在），fanout 判断不触发（spec 三档映射：EXTRACTED → 不标注）。"""
    from graphify.serve import _blast_radius_lines
    DG = _fanout_digraph()
    DG.add_edge("function:c11", "function:f01", relation="calls",
                confidence="EXTRACTED", resolved_by="qualified-name",
                source_file="pkg/caller.py", source_location="L10")
    lines, total = _blast_radius_lines(DG, "function:f01", "in", 2, 50, "")
    body = "\n".join(lines)
    assert "fanout" not in body, body            # EXTRACTED 不展开
    assert "resolved_by=qualified-name" in body, body  # 数据读出仍可见
    assert "[dispatch" not in body, body

def test_blast_radius_contains_edge_no_dispatch_annotation():
    """I1：contains 边在闭包内无标注——非 calls 边不标 resolved_by、不 fanout
    （god node 噪声根除：52 行中 44 行）。"""
    from graphify.serve import _blast_radius_lines
    DG = nx.DiGraph()
    DG.add_node("class:b01", kind="class", label="pkg.Base")
    DG.add_node("method:b1", kind="method", label="pkg.Base.handle")
    DG.add_edge("class:b01", "method:b1", relation="contains", source_file="pkg/base.py",
                confidence="EXTRACTED", resolved_by="qualified-name")
    lines, total = _blast_radius_lines(DG, "class:b01", "out", 2, 50, "")
    body = "\n".join(lines)
    assert "resolved_by" not in body and "dispatch" not in body and "fanout" not in body, body

def test_blast_radius_fanout_out_direction_lists_subclass_overrides():
    """Task 08 评审 F4（二轮 I1）：out 方向 fanout 定序回归锁——caller 调 Base.handle，
    ChildA/ChildB 覆写，从 out 方向（direction=out, depth=2）断言输出含子类展开。
    C1 定序变体若回归（callee 误取调用侧/入侧），展开会落到 caller 类层级或降级
    'no owning class'，静默逃逸——此锁防漏。"""
    from graphify.serve import _blast_radius_lines
    DG = _dispatch_digraph()
    lines, total = _blast_radius_lines(DG, "function:c11", "out", 2, 50, "")
    body = "\n".join(lines)
    assert "resolved_by=instance-method" in body, body
    assert "pkg.ChildA.handle" in body and "pkg.ChildB.handle" in body, body
    assert "no owning class" not in body

def test_blast_radius_fanout_limited_by_edge_kinds():
    """M1b：edge_kinds=['calls'] 滤掉 contains/extends → fanout 展开受限，降级 note 用
    'limited by edge_kinds filter' 而非 'no owning class'（区分"被过滤"与"无所属类"）。"""
    from graphify.serve import _blast_radius_lines
    DG = _dispatch_digraph()
    # 过滤后只剩 calls（contains/extends 缺席）→ fanout_limited=True
    DGF = nx.DiGraph()
    DGF.add_nodes_from(DG.nodes(data=True))
    for u, v, d in DG.edges(data=True):
        if str(d.get("relation", "")).lower() == "calls":
            DGF.add_edge(u, v, **d)
    lines, _ = _blast_radius_lines(DGF, "function:f01", "in", 2, 50, "", fanout_limited=True)
    body = "\n".join(lines)
    assert "limited by edge_kinds filter" in body, body
    assert "no owning class" not in body
    # 完整图（无过滤）→ fanout 正常展开 ChildA/ChildB
    lines2, _ = _blast_radius_lines(DG, "function:f01", "in", 2, 50, "")
    body2 = "\n".join(lines2)
    assert "pkg.ChildA.handle" in body2 and "pkg.ChildB.handle" in body2, body2

def test_blast_radius_both_truncation_labels_both_directions():
    """M1a：both 方向截断行标注 by degree across both directions（in/out 并集语义
    对称性透明化）；in 方向仍用 by degree（不带 both 标注）。"""
    from graphify.serve import _blast_radius_lines
    # L-B（补充审核）：下面 any(...) 截断行断言依赖 _fanout_digraph() 的 total=4 > top_k=1
    # （f01 both depth=2 闭包 4 节点：in {c11,b01,a01,a02} + out ∅，top 1 截断才产截断行）；
    # 改动 fixture 节点数需同步检查此断言（节点数减到 ≤top_k 时无截断行，any 落空）。
    DG = _fanout_digraph()
    lines, total = _blast_radius_lines(DG, "function:f01", "both", 2, 1, "")
    assert total > 1
    assert any("by degree across both directions" in ln for ln in lines)
    lines, total = _blast_radius_lines(DG, "function:f01", "in", 2, 1, "")
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

def test_get_neighbors_tool_description_annotates_semantic_change():
    """工具描述诚实标注语义变化：外部解析器判定 → 提取期原生推断（dispatch 退役）."""
    from graphify.serve import _all_tool_schemas
    desc = next(s["description"] for s in _all_tool_schemas() if s["name"] == "get_neighbors")
    assert "extraction-time inference" in desc, desc
    assert "resolved_by" in desc, desc
    assert "subclass overrides" in desc, desc
