"""_is_file_node 对 kind='file' 短路测试."""
import networkx as nx
from graphify.analyze import _is_file_node


def test_file_node_by_kind_returns_true():
    G = nx.Graph()
    G.add_node("file:src/app.py", label="app.py", source_file="src/app.py", kind="file")
    assert _is_file_node(G, "file:src/app.py") is True


def test_file_node_kind_priority_over_label():
    G = nx.Graph()
    G.add_node("n1", label="ambiguous", source_file="src/x.py", kind="file")
    assert _is_file_node(G, "n1") is True


def test_non_file_node_unaffected():
    G = nx.Graph()
    G.add_node("func1", label="doThing", source_file="src/x.py", kind="function")
    assert _is_file_node(G, "func1") is False
