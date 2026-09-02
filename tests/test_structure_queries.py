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

def test_auto_mode_package_level_main_py(tmp_path):
    """M1（owner 审核）：包内 __main__.py（非根目录）→ auto 探测走 application 分支。
    全图扫 source_file.endswith('__main__.py') 任意深度——Python 惯例是包内 __main__.py
    （fork 自己是 graphify/__main__.py），根目录 project_root/__main__.py 形态近乎不出现；
    修复前根目录单点检查对该形态永不命中，39 个同名 main 成唯一工作路径 = 入口过宽。"""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__main__.py").write_text("def main(): ...\n", encoding="utf-8")
    G = _G([("main", "util")],
           {"main": "function", "util": "function", "orphan": "function"})
    for n in G.nodes:
        G.nodes[n]["source_file"] = "pkg/__main__.py" if n == "main" else "pkg/mod.py"
    r = find_dead_code(G, project_root=tmp_path)
    assert r["entry_mode"] == "application"      # 走 __main__.py 分支，非短名 main 判定
    assert "main" not in r["unreachable"]        # 入口可达
    assert "orphan" in r["unreachable"]

def test_import_derivation_pass_through_evidence():
    """SDD review Minor-2：import 推导隔离锁定（此前只在真实图 fork 实测验证）。
    kind='import' 节点本身不算不可达、不进符号总数分母；relation='imports' 边是使用
    证据——入口经 import 边可达的被导入符号不算死代码；真孤儿仍报。"""
    G = nx.DiGraph()
    G.add_node("main", kind="function", label="main")
    G.add_node("imp", kind="import", label="pkg.mod")     # import 语句节点（仅 contains 挂载）
    G.add_node("used", kind="function", label="used()")   # 被入口 import 的符号
    G.add_node("dead", kind="function", label="dead()")   # 真孤儿
    G.add_edge("main", "imp", relation="contains")        # import 节点经 contains 挂文件下
    G.add_edge("main", "used", relation="imports")        # import 边：使用证据
    r = find_dead_code(G, entry_mode="application")
    assert "imp" not in r["unreachable"]      # import 节点不算不可达
    assert "used" not in r["unreachable"]     # import 边使 used 可达（透传证据）
    assert "dead" in r["unreachable"]         # 孤儿仍报
    assert r["scanned"] == 3                  # 分母=3 符号（import 节点不入分母）
