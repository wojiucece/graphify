"""06 票 Seam 3 工具/查询面：get_node 四档在新链路（FTS 缓存 + graph.json）语义不变。

经模块级 _get_node_tool(G, active_graph_path, arguments) 直接驱动（_shortest_path_text
先例——不经 mcp，无 HTTP 依赖）。fixture 生产派生：真实提取器产节点 + rebuild_fts 缓存。

覆盖：
- signature 档：Signature:/Doc: 行来自 .fts-index.db nodes 表（新契约字段）
- body 档：字节精确切片（end_byte 原语）——同行尾随代码/其他函数不收
- body+context 档：±3 邻接签名摘要（FTS 缓存签名，无 __cg 回退）
- none 档：名片无 Signature:/Doc:/Code: 行（回归锚点）
- 语义/概念节点显式 body → absent（source_location=None 无切片面）
- 无 __cg 消歧后缀形态；含 __cg 后缀的 legacy id 亦直接命中（无基 id 回退）
- 首次调用无缓存：惰性 ensure_fts 重建 → freshness=fresh（诚实：缓存已更新）
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import pytest

import fts_cache as fc  # noqa: E402
from graphify import serve as serve_mod  # noqa: E402
from graphify.extract import extract_python  # noqa: E402

SOURCE = (
    "import os\n"
    "\n"
    "\n"
    "def target_fn(x):\n"
    "    \"\"\"Docs here.\"\"\"\n"
    "    return x + 1\n"
    "\n"
    "\n"
    "def other():\n"
    "    pass\n"
)


def _mk_proj(tmp_path, semantic_label="Some Concept", cg_suffix=False):
    """mini 项目：真实提取器产节点（生产派生）+ 可选语义概念节点 + rebuild_fts 缓存。
    cg_suffix=True 时给 target_fn id 加 __cg 后缀（legacy 折叠 id 形态，验证直接命中）。"""
    proj = tmp_path / "proj"
    (proj / "pkg").mkdir(parents=True)
    f = proj / "pkg" / "mod.py"
    f.write_text(SOURCE, encoding="utf-8")
    res = extract_python(f)
    nodes = []
    for n in res["nodes"]:
        if cg_suffix and n.get("label") == "target_fn()":
            n = dict(n, id=n["id"] + "__cg7")
        nodes.append(n)
    nodes.append({
        "id": "concept:some_concept", "label": semantic_label,
        "file_type": "concept", "_origin": "semantic",
        "source_file": "docs/concepts", "source_location": None,
    })
    out = proj / "graphify-out"
    out.mkdir()
    g = {"directed": False, "multigraph": False, "graph": {},
         "nodes": nodes, "links": res["edges"]}
    gp = out / "graph.json"
    gp.write_text(json.dumps(g, ensure_ascii=False), encoding="utf-8")
    return proj, gp


def _load(graph_path):
    return serve_mod._load_graph(str(graph_path))


def _node_id(G, label_frag):
    return next(nid for nid, d in G.nodes(data=True) if label_frag in str(d.get("label", "")))


def test_get_node_signature_tier_carries_contract_fields(tmp_path):
    """signature 档：Signature:/Doc: 来自 FTS 缓存新契约字段（名字不进签名 → def 重构）。"""
    proj, gp = _mk_proj(tmp_path)
    fc.rebuild_fts(gp, proj / "graphify-out" / ".fts-index.db")
    G = _load(gp)
    text, found, scanned, override = serve_mod._get_node_tool(G, str(gp), {"label": "target_fn"})
    assert found and override is None
    assert "Node: target_fn()" in text
    assert "Signature: def target_fn(x)" in text
    assert "Doc: Docs here." in text
    assert "__cg" not in text


def test_get_node_body_byte_precise_slice(tmp_path):
    """body 档：字节精确切片（end_byte 原语）——只含目标函数，不含 other()。"""
    proj, gp = _mk_proj(tmp_path)
    fc.rebuild_fts(gp, proj / "graphify-out" / ".fts-index.db")
    G = _load(gp)
    text, found, scanned, override = serve_mod._get_node_tool(
        G, str(gp), {"label": "target_fn", "include_source": "body"})
    assert found and override is None
    body = text.split("Code:")[1]
    assert "def target_fn(x):" in body
    assert '"""Docs here."""' in body
    assert "return x + 1" in body
    assert "other" not in body          # 字节精确：未收 other()


def test_get_node_body_plus_context_neighbors(tmp_path):
    """body+context 档：±3 邻接签名摘要来自 FTS 缓存（邻居签名 def 重构无括号后缀）。"""
    proj, gp = _mk_proj(tmp_path)
    fc.rebuild_fts(gp, proj / "graphify-out" / ".fts-index.db")
    G = _load(gp)
    text, found, scanned, override = serve_mod._get_node_tool(
        G, str(gp), {"label": "target_fn", "include_source": "body+context"})
    assert found and override is None
    assert "Code:" in text
    assert "Context (1-hop neighbors):" in text
    assert "__cg" not in text


def test_get_node_none_tier_is_pre_extension_anchor(tmp_path):
    """none 档：名片无 Signature:/Doc:/Code: 行（与扩展前逐字节一致）。"""
    proj, gp = _mk_proj(tmp_path)
    fc.rebuild_fts(gp, proj / "graphify-out" / ".fts-index.db")
    G = _load(gp)
    text, found, scanned, override = serve_mod._get_node_tool(
        G, str(gp), {"label": "target_fn", "include_source": "none"})
    assert found and override is None
    assert "Signature:" not in text and "Doc:" not in text and "Code:" not in text


def test_get_node_semantic_node_body_is_absent(tmp_path):
    """语义节点（source_location=None，无切片面）显式 body → absent（旧链路等价）。"""
    proj, gp = _mk_proj(tmp_path)
    fc.rebuild_fts(gp, proj / "graphify-out" / ".fts-index.db")
    G = _load(gp)
    text, found, scanned, override = serve_mod._get_node_tool(
        G, str(gp), {"label": "some concept", "include_source": "body"})
    assert found
    assert override == "absent"
    assert "Code:" not in text


def test_get_node_legacy_cg_id_direct_hit(tmp_path):
    """含 __cg 后缀的 legacy id 亦直接命中（缓存与图同源，id 逐字一致）——06 决议删除
    基 id 回退点查后仍工作。legacy 图的节点 id 自身就是 __cg 形态，ID: 行如实显示；
    新管线原生 id 的"响应无后缀"由其余用例锁定（见 signature 档用例）。"""
    proj, gp = _mk_proj(tmp_path, cg_suffix=True)
    fc.rebuild_fts(gp, proj / "graphify-out" / ".fts-index.db")
    G = _load(gp)
    text, found, scanned, override = serve_mod._get_node_tool(
        G, str(gp), {"label": "target_fn", "include_source": "body"})
    assert found and override is None
    assert "Code:" in text and "def target_fn(x):" in text
    assert "return x + 1" in text


def test_get_node_first_call_builds_cache_and_reports_fresh(tmp_path):
    """首次调用无缓存：get_node 惰性 ensure_fts 重建 → freshness=fresh（诚实：缓存已更新）。
    经 call_tool 同款装配（_apply_envelope + _derive_freshness）验证信封 verdict/freshness。"""
    proj, gp = _mk_proj(tmp_path)
    assert not (proj / "graphify-out" / ".fts-index.db").exists()   # 无缓存
    G = _load(gp)
    result = serve_mod._get_node_tool(G, str(gp), {"label": "target_fn"})
    assert (proj / "graphify-out" / ".fts-index.db").exists()       # 惰性重建落盘
    state = proj / "graphify-out" / ".rebuild-state.json"
    state.write_text(json.dumps({"schema": 1, "phase": "complete"}), encoding="utf-8")
    out = serve_mod._apply_envelope("get_node", result, serve_mod._derive_freshness(state))
    meta = json.loads(out.rstrip("\n").split("\n")[-1].removeprefix("_meta: "))
    assert meta["verdict"] == "ok" and meta["freshness"] == "fresh"
    assert "Signature: def target_fn(x)" in out


def test_ensure_fts_retry_backoff_on_lock(monkeypatch):
    """连接纪律决策（Task 05 ⚠️③ checklist）：serve 侧短连接 + os.replace 锁
    （PermissionError/WinError 5）指数退避重试兜底——首次失败后重试成功。"""
    from graphify.serve import _ensure_fts_retry
    fts = serve_mod._fts_cache()
    calls = {"n": 0}
    def flaky(graph_path, fts_path):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError(5, "Access is denied")
        return True
    monkeypatch.setattr(fts, "ensure_fts", flaky)
    assert _ensure_fts_retry("g.json", "f.db") is True
    assert calls["n"] == 2


def test_ensure_fts_retry_exhaustion_raises(monkeypatch):
    """重试耗尽（5 次全锁）→ 向上抛——缓存构建失败是事实层派生失败，诚实暴露给
    get_node 的 except 降级路径（不静默吞错）。"""
    from graphify.serve import _ensure_fts_retry
    fts = serve_mod._fts_cache()
    def always_locked(graph_path, fts_path):
        raise PermissionError(5, "Access is denied")
    monkeypatch.setattr(fts, "ensure_fts", always_locked)
    with pytest.raises(PermissionError):
        _ensure_fts_retry("g.json", "f.db")


def test_get_node_cache_stale_reports_stale_index(tmp_path):
    """缓存落后于事实层（graph.json 更新于缓存）→ freshness=stale_index 诚实标注。"""
    proj, gp = _mk_proj(tmp_path)
    fc.rebuild_fts(gp, proj / "graphify-out" / ".fts-index.db")
    # 事实层更新（追加节点）→ 缓存指纹失配
    g = json.loads(gp.read_text(encoding="utf-8"))
    g["nodes"].append({"id": "extra", "label": "extra_sym", "file_type": "code",
                       "source_file": "pkg/mod.py", "source_location": "L9:C1"})
    gp.write_text(json.dumps(g, ensure_ascii=False), encoding="utf-8")
    state = proj / "graphify-out" / ".rebuild-state.json"
    state.write_text(json.dumps({"schema": 1, "phase": "complete"}), encoding="utf-8")
    # 未触发重建的查询（graph_stats 不消费缓存）→ freshness 诚实判 stale_index
    from graphify.serve import _derive_freshness
    assert _derive_freshness(state) == "stale_index"
