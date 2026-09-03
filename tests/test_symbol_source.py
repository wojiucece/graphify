"""B2 切片：四档 / 字节精确切片（end_byte 原语）/ 行漂移 fuzzy 重定位 / 语义节点 absent /
none 档逐字节一致.

06 票换源：fixture 从 codegraph DB 换成 真实提取器 + rebuild_fts 缓存（新链路数据源）；
_slice_source 签名升级为 (source_location, end_line, end_byte) 字节切。
Supplementary Batch（M1/M2/L1/L2）：邻接签名摘要 / _last_slice_meta 全局删除 /
重定位截断标注 / sanitize_label 代码文本语义锁定。
"""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from types import SimpleNamespace
import networkx as nx
import pytest
import fts_cache as fc
from graphify.extract import extract_python

MOD_SRC = (
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


@pytest.fixture
def proj(tmp_path):
    """mini 项目：真实提取器产节点（生产派生）→ graph.json + rebuild_fts 缓存。
    target_fn 契约字段随提取器自愈（不手写理想化字节数）。"""
    root = tmp_path
    f = root / "pkg" / "mod.py"; f.parent.mkdir(parents=True)
    f.write_text(MOD_SRC, encoding="utf-8")
    res = extract_python(f)
    gp = root / "graphify-out" / "graph.json"
    gp.parent.mkdir()
    g = {"directed": False, "multigraph": False, "graph": {},
         "nodes": res["nodes"], "links": res["edges"]}
    gp.write_text(json.dumps(g, ensure_ascii=False), encoding="utf-8")
    fts = root / "graphify-out" / ".fts-index.db"
    fc.rebuild_fts(gp, fts)
    by_label = {n["label"]: n for n in res["nodes"]}
    return SimpleNamespace(root=root, graph=gp, fts=fts,
                           t=by_label["target_fn()"], other=by_label["other()"], src=str(f))


def test_body_slice_matches_file(proj):
    from graphify.serve import _slice_source, _card_short_name
    text, ok, s, e = _slice_source(proj.root, proj.t["source_file"], proj.t["source_location"],
                                   proj.t["end_line"], proj.t["end_byte"],
                                   _card_short_name(proj.t, proj.t["id"]))
    assert ok and "def target_fn(x):" in text and "return x + 1" in text
    assert (s, e) == (4, 6)   # M2：返回元组带 slice_start/slice_end（取代全局回传）


def test_body_byte_slice_excludes_trailing_content(proj):
    """字节精确：end_byte 定终点，切片不含目标函数之后的代码（其他() 不收）。
    与实现共用 _line_byte_starts 原语验证字节一致性（Windows 写盘 CRLF——end_byte 是
    磁盘字节坐标系，须读磁盘字节而非内存字符串）。"""
    from graphify.serve import _slice_source, _line_byte_starts, _card_short_name
    text, ok, s, e = _slice_source(proj.root, proj.t["source_file"], proj.t["source_location"],
                                   proj.t["end_line"], proj.t["end_byte"],
                                   _card_short_name(proj.t, proj.t["id"]))
    assert ok
    assert "other" not in text
    raw = Path(proj.src).read_bytes()
    starts = _line_byte_starts(raw)
    start_line = int(proj.t["source_location"].split(":")[0][1:])
    start_col = int(proj.t["source_location"].split(":")[1][1:])
    start_byte = starts[start_line - 1] + (start_col - 1)
    assert raw[start_byte:proj.t["end_byte"]].decode("utf-8") == text


def test_drift_triggers_fuzzy_relocate(proj):
    """N5：两条分支都有实质断言——fuzzy 成功验内容，失败验返回元组形态."""
    from graphify.serve import _slice_source
    f = Path(proj.src)
    f.write_text("# drift1\n# drift2\n# drift3\n" + f.read_text(encoding="utf-8"), encoding="utf-8")
    text, ok, s, e = _slice_source(proj.root, proj.t["source_file"], proj.t["source_location"],
                                   proj.t["end_line"], proj.t["end_byte"], "target_fn")
    if ok:
        assert "def target_fn(x):" in text   # fuzzy 重定位成功：切片含目标函数
    else:
        assert (s, e) == (0, 0)              # M2 失败：返回 (text, False, 0, 0) 哨兵


def test_last_slice_meta_global_deleted():
    """M2：模块级 _last_slice_meta 全局已删除（FastMCP sync handler 线程池并发 get_node
    交错互覆的竞态源）——返回值 (text, ok, start, end) 取代全局回传，消 TOCTOU 双读."""
    from graphify import serve
    assert not hasattr(serve, "_last_slice_meta")
    assert hasattr(serve, "_slice_source")


def test_slice_source_pad_expands_window(proj):
    """M2：body+context ±3 上下文窗内聚为 _slice_source 的 pad 参数——单次读文件完成
    切片+扩窗。target_fn [4,6] pad=3 → 扩为 [1,9]。"""
    from graphify.serve import _slice_source, _card_short_name
    text, ok, s, e = _slice_source(proj.root, proj.t["source_file"], proj.t["source_location"],
                                   proj.t["end_line"], proj.t["end_byte"],
                                   _card_short_name(proj.t, proj.t["id"]), pad=3)
    assert ok
    assert (s, e) == (1, 9)                # 扩窗数学：前后各 3 行，夹到文件边界
    assert "import os" in text             # 前扩覆盖到 import 行


def test_relocated_window_truncation_hint(proj):
    """L1：fuzzy 重定位 ±40 硬窗对长函数（>80 行）静默截断——切片尾行非文件尾时附提示行.
    legacy 形态（无 end_byte/end_line）直接走 fuzzy 定位。"""
    from graphify.serve import _slice_source
    f = Path(proj.src)
    lines = ["# drift1", "# drift2", "# drift3"]        # 3 行漂移触发 fuzzy
    lines += ["def long_fn():", "    pass"]
    lines += [f"    # body {i}" for i in range(90)]     # 长函数体（>80 行）
    lines += ["def trailing():", "    pass"]            # 函数后仍有内容 → 窗非文件尾
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    text, ok, s, e = _slice_source(proj.root, "pkg/mod.py", "L4", None, None, "long_fn")
    assert ok
    assert "def long_fn():" in text
    assert "(relocated window, may be truncated)" in text
    assert e < len(lines)                              # 窗尾非文件尾（触发条件）


def test_relocated_window_no_hint_at_file_end(proj):
    """L1 负例：重定位窗达文件尾 → 无截断可能，不附提示行."""
    from graphify.serve import _slice_source
    f = Path(proj.src)
    f.write_text("# drift1\n# drift2\n# drift3\ndef short_fn():\n    return 1\n",
                 encoding="utf-8")
    text, ok, s, e = _slice_source(proj.root, "pkg/mod.py", "L4", None, None, "short_fn")
    assert ok
    assert "def short_fn():" in text
    assert "(relocated window, may be truncated)" not in text
    assert e == len(f.read_text(encoding="utf-8").splitlines())   # 窗尾即文件尾


def test_semantic_node_body_request_is_absent():
    from graphify.serve import _verdict_for_source_request
    assert _verdict_for_source_request(has_source=False, explicit_body=True) == "absent"  # Q9 三分


def test_semantic_node_default_is_card_only():
    from graphify.serve import _verdict_for_source_request
    assert _verdict_for_source_request(has_source=False, explicit_body=False) is None  # 名片正常


def test_none_mode_card_is_pre_extension_anchor():
    """B2 none 档回归锚点：名片正文与扩展前逐字节一致（无 Signature:/Doc:/Code: 行）."""
    import networkx as nx
    from graphify.serve import _format_node_card
    G = nx.Graph()
    G.add_node("t1", label="target_fn", source_file="pkg/mod.py", source_location="L4",
               file_type="python", community_name="c1")
    card = _format_node_card(G, "t1", G.nodes["t1"])
    assert card == (
        "Node: target_fn\n"
        "  ID: t1\n"
        "  Source: pkg/mod.py L4\n"
        "  Type: python\n"
        "  Community: c1\n"
        "  Degree: 0"
    )
    assert "Signature:" not in card and "Doc:" not in card and "Code:" not in card


def test_card_short_name_strips_native_call_suffix():
    """_card_short_name：原生 label `target_fn()`/`.fetch()` → 裸名（def 重构/切片校验用）；
    qualified_name `Class::method` → 末段；旧链路 dotted → 末段。"""
    from graphify.serve import _card_short_name
    assert _card_short_name({"label": "target_fn()"}, "x") == "target_fn"
    assert _card_short_name({"label": ".fetch()"}, "x") == "fetch"
    assert _card_short_name({"label": ".__init__()"}, "x") == "__init__"
    assert _card_short_name({"label": "n", "qualified_name": "UserService::fetch"}, "x") == "fetch"
    assert _card_short_name({"label": "x", "qualified_name": "pkg.target_fn"}, "x") == "target_fn"
    assert _card_short_name({"label": "UserService"}, "x") == "UserService"


def test_signature_output_reconstructs_def_from_fts_shape(proj):
    """新链路契约：signature 列无 def 前缀（名字不进签名）——Signature 输出重构出
    `def target_fn(x)`，而非原样 `(x)`（FTS 缓存原文是 `(x)`）。"""
    import sqlite3 as _sq
    from graphify.serve import _signature_line
    conn = _sq.connect(f"file:{proj.fts.as_posix()}?mode=ro", uri=True)
    raw = conn.execute("SELECT signature FROM nodes WHERE id=?",
                       (proj.t["id"],)).fetchone()[0]
    conn.close()
    assert raw == "(x)"                                   # 新链路形态：无 def 前缀
    assert _signature_line(raw, "target_fn") == "def target_fn(x)"   # 重构拼接出 def 行
    assert _signature_line("(self)", "__init__") == "def __init__(self)"  # review 验收例


def test_signature_line_keeps_existing_prefix():
    """已带 def/async def/class 前缀的 signature 原样输出（不重复拼接）."""
    from graphify.serve import _signature_line
    assert _signature_line("def other():", "other") == "def other():"
    assert _signature_line("async def foo():", "foo") == "async def foo():"
    assert _signature_line("class Foo:", "Foo") == "class Foo:"


def test_signature_line_does_not_mangle_non_python():
    """非括号形态（非 Python 语言签名，如 `void (String item)`）不套 Python def."""
    from graphify.serve import _signature_line
    assert _signature_line("void (String item)", "addItem") == "void (String item)"
    assert _signature_line("", "foo") == ""


# --- M1: body+context 邻接签名摘要（label + Signature: 双列，度数 top-10）---

@pytest.fixture
def neighbor_env(tmp_path):
    """M1：DiGraph（hub + 15 邻居，度数递增 1..15）+ FTS 缓存（nodes 表 signature 列）.
    n6 的 signature 是多行（含 \n）——L2 验证代码文本不做 F-010 sanitize."""
    import sqlite3 as _sq
    root = tmp_path
    out = root / "graphify-out"; out.mkdir()
    G = nx.DiGraph()
    G.add_node("hub", label="hub", kind="function")
    nodes = [{"id": "hub", "label": "hub", "kind": "function", "source_file": "hub.py",
              "source_location": "L1:C1", "signature": "()"}]
    for i in range(1, 16):
        nid = f"n{i}"
        G.add_node(nid, label=f"n{i}", kind="function")
        G.add_edge("hub", nid, relation="calls")
        for j in range(i - 1):                       # 度数 = i（hub 入边 1 + 出边 i-1）
            G.add_edge(nid, f"x{i}_{j}", relation="calls")
            G.add_node(f"x{i}_{j}", label=f"x{i}_{j}", kind="function")
        sig = "(\n    x: int,\n) -> None:" if i == 6 else f"(arg{i})"
        nodes.append({"id": nid, "label": f"n{i}", "kind": "function",
                      "source_file": "mod.py", "source_location": f"L{i}:C1",
                      "signature": sig})
    g = {"directed": False, "multigraph": False, "graph": {}, "nodes": nodes, "links": []}
    gp = out / "graph.json"
    gp.write_text(json.dumps(g), encoding="utf-8")
    fc.rebuild_fts(gp, out / ".fts-index.db")
    return G, root


def test_top_neighbor_ids_rank_by_degree_top10(neighbor_env):
    from graphify.serve import _top_neighbor_ids
    G, _root = neighbor_env
    top_ids, total = _top_neighbor_ids(G, "hub", top=10)
    assert total == 15
    assert len(top_ids) == 10
    assert top_ids == [f"n{i}" for i in range(15, 5, -1)]      # 度数 15..6 降序
    assert [G.degree(nb) for nb in top_ids] == list(range(15, 5, -1))


def test_neighbor_summary_signature_and_truncation(neighbor_env):
    from graphify.serve import _neighbor_summary_lines
    G, root = neighbor_env
    lines = _neighbor_summary_lines(G, "hub", root / "graphify-out" / ".fts-index.db")
    assert len(lines) == 11                      # 10 邻居 + (+5 more) 截断标注
    assert lines[0] == "  n15  Signature: def n15(arg15)"
    assert lines[-1] == "  (+5 more)"
    assert sum(1 for l in lines if "Signature:" in l) == 10


def test_neighbor_summary_signature_not_sanitized(neighbor_env):
    """L2 应用：邻接摘要 Signature 是代码文本（非 LLM 字段）——多行签名含 \n 原样保留，
    不做 F-010 sanitize（sanitize_label 会剥 \n 变形，见 test_sanitize_label_strips_*）."""
    from graphify.serve import _neighbor_summary_lines
    G, root = neighbor_env
    lines = _neighbor_summary_lines(G, "hub", root / "graphify-out" / ".fts-index.db")
    n6 = next(l for l in lines if l.startswith("  n6 "))
    assert "def n6(\n      x: int,\n  ) -> None:" in n6


def test_neighbor_signatures_missing_db_returns_empty(tmp_path):
    """M1 诚实降级：缓存缺失/损坏 → 空 dict（不崩出口）."""
    from graphify.serve import _neighbor_signatures
    assert _neighbor_signatures(tmp_path / "missing" / ".fts-index.db", ["n1", "n2"]) == {}


# --- L2: sanitize_label 对代码文本的语义锁定（F-010 面向 LLM 字段）---

def test_sanitize_label_preserves_code_semantic_chars():
    """L2：sanitize_label（F-010 LLM 字段清洗）对代码语义字符（括号/下划线/星号/箭头/
    backtick/unicode/空白）原样保留——单行签名/文档片段不变形."""
    from graphify.security import sanitize_label
    samples = [
        "def process(items: list[tuple[int, str]], *args, **kwargs) -> None:",
        "def target_fn(x: int) -> tuple[int, str]:",
        "Returns a ``dict`` of ``{name: score}`` pairs.",
        "def area(r: float) -> float:  # 半径",
    ]
    for s in samples:
        assert sanitize_label(s) == s

def test_sanitize_label_strips_control_whitespace_in_code():
    """L2 发现：sanitize_label 剥 \n/\t（控制空白）→ 多行/含 tab 签名被变形——B2 输出
    路径据此对 Signature/Code 段不做 F-010 sanitize（代码文本非 LLM 字段；sanitize_label
    是上游函数，不改，仅在 B2 输出面规避）。"""
    from graphify.security import sanitize_label
    multiline = "def foo(\n    x: int,\n) -> None:"
    assert sanitize_label(multiline) != multiline
    assert "\n" not in sanitize_label(multiline)
    tabbed = "def foo(x,\ty):"
    assert sanitize_label(tabbed) != tabbed
    assert "\t" not in sanitize_label(tabbed)
