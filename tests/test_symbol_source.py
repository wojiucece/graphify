"""B2 切片：四档 / 行漂移 fuzzy 重定位 / 语义节点 absent / none 档逐字节一致."""
import json, sqlite3, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
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
    text, ok = _slice_source(proj, "pkg/mod.py", 4, 6, "target_fn")
    assert ok and "def target_fn(x):" in text and "return x + 1" in text

def test_drift_triggers_fuzzy_relocate(proj):
    """N5：两条分支都有实质断言——fuzzy 成功验内容，失败验标志."""
    from graphify import serve
    from graphify.serve import _slice_source
    # 模拟漂移：文件头部插 3 行，DB 行区间失效
    f = proj / "pkg" / "mod.py"
    f.write_text("# drift1\n# drift2\n# drift3\n" + f.read_text(encoding="utf-8"), encoding="utf-8")
    text, ok = _slice_source(proj, "pkg/mod.py", 4, 6, "target_fn")
    if ok:
        assert "def target_fn(x):" in text   # fuzzy 重定位成功：切片含目标函数
    else:
        assert serve._last_slice_meta.get("slice_verified") is False   # 失败：标志在位

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
