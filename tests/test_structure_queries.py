"""C1：应用型入口闭包 / 库型公开导出面 / import 推导 / >50% 闸门."""
import networkx as nx, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from structure_queries import find_dead_code

def _G(edges, kinds):
    G = nx.DiGraph()
    for n, k in kinds.items():
        G.add_node(n, kind=k)
    G.add_edges_from(edges)
    return G

def test_application_entry_closure():
    # main -> util -> helper 链 + 孤儿 orphan
    G = _G([("main", "util"), ("util", "helper")],
           {"main": "function", "util": "function", "helper": "function", "orphan": "function"})
    r = find_dead_code(G, entry_mode="application")
    assert "orphan" in r["unreachable"] and "helper" not in r["unreachable"]

def test_library_entry_uses_public_exports():
    # 库型：__init__ re-export a/b；main() 存在但不是入口
    G = _G([("__init__", "a"), ("__init__", "b")],
           {"__init__": "file", "a": "function", "b": "function",
            "hidden": "function", "main": "function"})
    r = find_dead_code(G, entry_mode="library")
    assert "hidden" in r["unreachable"] and "a" not in r["unreachable"]

def test_gate_over_50pct_downgrades():
    G = _G([], {f"n{i}": "function" for i in range(10)})   # 全孤儿
    r = find_dead_code(G, entry_mode="application")
    assert r["gate_failed"] is True and r["degraded_to"] == "orphan_hint"

def test_auto_mode_detects_application_entry():
    """N3：auto 探测路径必须有测试——label 末段匹配 main（合并图节点无 name 属性）."""
    G = _G([("app.main", "util"), ("util", "helper")],
           {"app.main": "function", "util": "function", "helper": "function"})
    for n in G.nodes:
        G.nodes[n]["label"] = n          # 合并图节点以 label 承载符号名
    G.add_node("stray", kind="function", label="stray")   # 无入口可达的孤儿
    r = find_dead_code(G)   # 不传 entry_mode -> auto
    assert r["entry_mode"] == "application"
    assert "stray" in r["unreachable"] and "helper" not in r["unreachable"]

def test_find_dead_code_rejects_undirected_graph():
    """R4-1：生产 serve._load_graph 产 nx.Graph——无向传入必须 TypeError，
    防连通分量退化静默产出错误的"全可达"结果."""
    import pytest as _pt
    UG = nx.Graph(); UG.add_edge("main", "util")
    with _pt.raises(TypeError):
        find_dead_code(UG)
