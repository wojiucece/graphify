"""B2 切片：四档 / 行漂移 fuzzy 重定位 / 语义节点 absent / none 档逐字节一致.

Supplementary Batch（M1/M2/L1/L2）：邻接签名摘要 / _last_slice_meta 全局删除 /
重定位截断标注 / sanitize_label 代码文本语义锁定。"""
import json, sqlite3, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import networkx as nx
import pytest

@pytest.fixture
def proj(tmp_path):
    root = tmp_path
    f = root / "pkg" / "mod.py"; f.parent.mkdir(parents=True)
    f.write_text("import os\n\n\ndef target_fn(x):\n    \"\"\"doc.\"\"\"\n    return x + 1\n\n\ndef other():\n    pass\n", encoding="utf-8")
    db = root / ".codegraph" / "codegraph.db"; db.parent.mkdir()
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY, kind TEXT, name TEXT, "
                 "qualified_name TEXT, file_path TEXT, language TEXT, start_line INT, "
                 "end_line INT, docstring TEXT, signature TEXT)")
    # fixture 生产派生纪律（review fix）：t1 signature 贴真实 DB 形态——无 def 前缀，
    # fork 实测 7378/7378 函数/方法签名均为 `(self)`/`(x)` 形；t2 留一个已带 def 前缀
    # 的样例，验证名片不重复拼接。
    conn.execute("INSERT INTO nodes VALUES ('t1','function','target_fn','pkg.target_fn',"
                 "'pkg/mod.py','python',4,6,'doc.','(x)')")
    conn.execute("INSERT INTO nodes VALUES ('t2','function','other','pkg.other',"
                 "'pkg/mod.py','python',9,10,NULL,'def other():')")
    conn.commit(); conn.close()
    (root / "graphify-out").mkdir()
    (root / "graphify-out" / "graph.json").write_text(json.dumps(
        {"directed": False, "multigraph": False, "graph": {},
         "nodes": [{"id": "t1", "label": "target_fn", "source_file": "pkg/mod.py",
                    "source_location": "L4", "kind": "function"},
                   {"id": "t2", "label": "other", "source_file": "pkg/mod.py",
                    "source_location": "L9", "kind": "function"}], "links": []}), encoding="utf-8")
    return root

def test_body_slice_matches_file(proj):
    from graphify.serve import _slice_source
    text, ok, s, e = _slice_source(proj, "pkg/mod.py", 4, 6, "target_fn")
    assert ok and "def target_fn(x):" in text and "return x + 1" in text
    assert (s, e) == (4, 6)   # M2：返回元组带 slice_start/slice_end（取代全局回传）

def test_drift_triggers_fuzzy_relocate(proj):
    """N5：两条分支都有实质断言——fuzzy 成功验内容，失败验返回元组形态."""
    from graphify.serve import _slice_source
    # 模拟漂移：文件头部插 3 行，DB 行区间失效
    f = proj / "pkg" / "mod.py"
    f.write_text("# drift1\n# drift2\n# drift3\n" + f.read_text(encoding="utf-8"), encoding="utf-8")
    text, ok, s, e = _slice_source(proj, "pkg/mod.py", 4, 6, "target_fn")
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
    切片+扩窗（原实现切片后第二次重读文件扩 ±3 行，TOCTOU 双读）。fixture [4,6] pad=3
    → 扩为 [1,9]。"""
    from graphify.serve import _slice_source
    text, ok, s, e = _slice_source(proj, "pkg/mod.py", 4, 6, "target_fn", pad=3)
    assert ok
    assert (s, e) == (1, 9)                # 扩窗数学：前后各 3 行，夹到文件边界
    assert "import os" in text             # 前扩覆盖到 import 行

def test_relocated_window_truncation_hint(proj):
    """L1：fuzzy 重定位 ±40 硬窗对长函数（>80 行）静默截断——切片尾行非文件尾时附提示行."""
    from graphify.serve import _slice_source
    f = proj / "pkg" / "mod.py"
    lines = ["# drift1", "# drift2", "# drift3"]        # 3 行漂移触发 fuzzy
    lines += ["def long_fn():", "    pass"]
    lines += [f"    # body {i}" for i in range(90)]     # 长函数体（>80 行）
    lines += ["def trailing():", "    pass"]            # 函数后仍有内容 → 窗非文件尾
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    text, ok, s, e = _slice_source(proj, "pkg/mod.py", 1, 3, "long_fn")
    assert ok
    assert "def long_fn():" in text
    assert "(relocated window, may be truncated)" in text
    assert e < len(lines)                              # 窗尾非文件尾（触发条件）

def test_relocated_window_no_hint_at_file_end(proj):
    """L1 负例：重定位窗达文件尾 → 无截断可能，不附提示行."""
    from graphify.serve import _slice_source
    f = proj / "pkg" / "mod.py"
    f.write_text("# drift1\n# drift2\n# drift3\ndef short_fn():\n    return 1\n",
                 encoding="utf-8")
    text, ok, s, e = _slice_source(proj, "pkg/mod.py", 1, 3, "short_fn")
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

def test_signature_output_reconstructs_def_from_real_db_shape(proj):
    """fix(B2)：fixture signature 列贴生产形态（无 def 前缀，fork 实测 7378/7378）——
    Signature 输出须重构出 `def target_fn(x)`，而非原样 `(x)`（DB 原文是 `(x)`）."""
    import sqlite3 as _sq
    from graphify.serve import _signature_line
    conn = _sq.connect(proj / ".codegraph" / "codegraph.db")
    raw = conn.execute("SELECT signature FROM nodes WHERE id='t1'").fetchone()[0]
    conn.close()
    assert raw == "(x)"                                   # 生产形态：DB 无 def 前缀
    assert _signature_line(raw, "target_fn") == "def target_fn(x)"   # 重构拼接出 def 行
    assert _signature_line("(self)", "__init__") == "def __init__(self)"  # review 验收例

def test_signature_line_keeps_existing_prefix():
    """已带 def/async def/class 前缀的 signature 原样输出（不重复拼接）."""
    from graphify.serve import _signature_line
    assert _signature_line("def other():", "other") == "def other():"
    assert _signature_line("async def foo():", "foo") == "async def foo():"
    assert _signature_line("class Foo:", "Foo") == "class Foo:"

def test_prefixed_fixture_sample_no_double_concat(proj):
    """fixture 留有带 def 前缀的样例（t2/other）：原样输出，不重复拼接 def."""
    import sqlite3 as _sq
    from graphify.serve import _signature_line
    conn = _sq.connect(proj / ".codegraph" / "codegraph.db")
    raw = conn.execute("SELECT signature FROM nodes WHERE id='t2'").fetchone()[0]
    conn.close()
    assert raw == "def other():"
    assert _signature_line(raw, "other") == "def other():"

def test_signature_line_does_not_mangle_non_python():
    """非括号形态（非 Python 语言签名，如 `void (String item)`）不套 Python def."""
    from graphify.serve import _signature_line
    assert _signature_line("void (String item)", "addItem") == "void (String item)"
    assert _signature_line("", "foo") == ""


# --- M1: body+context 邻接签名摘要（label + Signature: 双列，度数 top-10）---

@pytest.fixture
def neighbor_env(tmp_path):
    """M1：DiGraph（hub + 15 邻居，度数递增 1..15）+ DB（nodes 表 signature 列）.
    n6 的 signature 是多行（含 \\n）——L2 验证代码文本不做 F-010 sanitize."""
    import sqlite3 as _sq
    root = tmp_path
    db = root / ".codegraph" / "codegraph.db"; db.parent.mkdir()
    conn = _sq.connect(db)
    conn.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY, signature TEXT)")
    G = nx.DiGraph()
    G.add_node("hub", label="hub", kind="function")
    for i in range(1, 16):
        nid = f"n{i}"
        G.add_node(nid, label=f"n{i}", kind="function")
        G.add_edge("hub", nid, relation="calls")
        for j in range(i - 1):                       # 度数 = i（hub 入边 1 + 出边 i-1）
            G.add_edge(nid, f"x{i}_{j}", relation="calls")
            G.add_node(f"x{i}_{j}", label=f"x{i}_{j}", kind="function")
        sig = "(\n    x: int,\n) -> None:" if i == 6 else f"(arg{i})"
        conn.execute("INSERT INTO nodes VALUES (?, ?)", (nid, sig))
    conn.execute("INSERT INTO nodes VALUES ('hub', '()')")
    conn.commit(); conn.close()
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
    lines = _neighbor_summary_lines(G, "hub", root / ".codegraph" / "codegraph.db")
    assert len(lines) == 11                      # 10 邻居 + (+5 more) 截断标注
    assert lines[0] == "  n15  Signature: def n15(arg15)"
    assert lines[-1] == "  (+5 more)"
    assert sum(1 for l in lines if "Signature:" in l) == 10

def test_neighbor_summary_signature_not_sanitized(neighbor_env):
    """L2 应用：邻接摘要 Signature 是代码文本（非 LLM 字段）——多行签名含 \\n 原样保留，
    不做 F-010 sanitize（sanitize_label 会剥 \\n 变形，见 test_sanitize_label_strips_*）."""
    from graphify.serve import _neighbor_summary_lines
    G, root = neighbor_env
    lines = _neighbor_summary_lines(G, "hub", root / ".codegraph" / "codegraph.db")
    n6 = next(l for l in lines if l.startswith("  n6 "))
    assert "def n6(\n      x: int,\n  ) -> None:" in n6

def test_neighbor_signatures_missing_db_returns_empty(tmp_path):
    """M1 诚实降级：DB 缺失/损坏 → 空 dict（不崩出口）."""
    from graphify.serve import _neighbor_signatures
    assert _neighbor_signatures(tmp_path / "missing" / "codegraph.db", ["n1", "n2"]) == {}


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
    """L2 发现：sanitize_label 剥 \\n/\\t（控制空白）→ 多行/含 tab 签名被变形——B2 输出
    路径据此对 Signature/Code 段不做 F-010 sanitize（代码文本非 LLM 字段；sanitize_label
    是上游函数，不改，仅在 B2 输出面规避）。"""
    from graphify.security import sanitize_label
    multiline = "def foo(\n    x: int,\n) -> None:"
    assert sanitize_label(multiline) != multiline
    assert "\n" not in sanitize_label(multiline)
    tabbed = "def foo(x,\ty):"
    assert sanitize_label(tabbed) != tabbed
    assert "\t" not in sanitize_label(tabbed)
