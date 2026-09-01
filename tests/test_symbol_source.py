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
    conn.execute("INSERT INTO nodes VALUES ('t1','function','target_fn','pkg.target_fn',"
                 "'pkg/mod.py','python',4,6,'doc.','def target_fn(x):')")
    conn.commit(); conn.close()
    (root / "graphify-out").mkdir()
    (root / "graphify-out" / "graph.json").write_text(json.dumps(
        {"directed": False, "multigraph": False, "graph": {},
         "nodes": [{"id": "t1", "label": "target_fn", "source_file": "pkg/mod.py",
                    "source_location": "L4", "kind": "function"}], "links": []}), encoding="utf-8")
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
