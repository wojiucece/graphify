"""C1：应用型入口闭包 / 库型公开导出面 / import 推导 / >50% 闸门.
C2：测试子图正向可达 / 30% 误报闸门（代理）/ 可配置 patterns."""
import networkx as nx, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from structure_queries import find_dead_code, untested_symbols

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
    （fork 自己是 graphify/__main__.py），根目录 project_root/__main__.py 形态近乎不出现。
    re-review Minor-1：入口节点命名 run（短名非 main/__main__）——若走短名回退分支（旧代码
    根目录检查失败后落短名/空 → library），entry_mode 断言即失败，真正 discriminate
    __main__.py 分支；命名 main 会让旧代码经短名回退也通过，守卫形同虚设。"""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__main__.py").write_text("def run(): ...\n", encoding="utf-8")
    G = _G([("run", "util")],
           {"run": "function", "util": "function", "orphan": "function"})
    for n in G.nodes:
        G.nodes[n]["source_file"] = "pkg/__main__.py" if n == "run" else "pkg/mod.py"
    r = find_dead_code(G, project_root=tmp_path)
    assert r["entry_mode"] == "application"      # 走 __main__.py 分支（短名回退无法到达）
    assert "run" not in r["unreachable"]         # 入口可达
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


# === C2: untested_symbols（Task 13）============================================


def _test_G():
    """C2 fixture：test_x.py -> fn（被测试覆盖）+ 无覆盖的 gy。"""
    G = nx.DiGraph()
    G.add_node("test_x", kind="file", label="tests/test_x.py", source_file="tests/test_x.py")
    G.add_node("fn", kind="function", label="fn()", source_file="x.py")
    G.add_node("gy", kind="function", label="gy()", source_file="y.py")
    G.add_edge("test_x", "fn", relation="imports")   # 测试文件 import 被测试符号
    return G


def test_untested_symbols_closure():
    """测试文件 test_x.py 正向可达集外 = 未覆盖：fn 被覆盖、gy 未覆盖。"""
    r = untested_symbols(_test_G())
    assert "gy" in r["untested"] and "fn" not in r["untested"]
    assert r["scanned"] == 2


def test_untested_import_derivation_pass_through():
    """C2 复用 C1 同一套闭包机制：测试文件经 import 边可达的符号算"已覆盖"
    （import 节点本身不算符号、不入分母）。"""
    G = nx.DiGraph()
    G.add_node("test_x", kind="file", source_file="tests/test_x.py")
    G.add_node("imp", kind="import", source_file="tests/test_x.py")  # import 语句节点
    G.add_node("used", kind="function", source_file="x.py")
    G.add_node("dead", kind="function", source_file="y.py")
    G.add_edge("test_x", "imp", relation="contains")
    G.add_edge("test_x", "used", relation="imports")   # 使用证据：used 被测试覆盖
    r = untested_symbols(G)
    assert "used" not in r["untested"]    # import 边透传
    assert "dead" in r["untested"]
    assert "imp" not in r["untested"]     # import 节点不算符号
    assert r["scanned"] == 2              # 分母=2 符号


def test_untested_gate_over_30pct_downgrades():
    """30% 误报闸门（代理）：未覆盖率 >30%（可达性证据稀疏 → 误报风险高）→
    gate_failed + degraded_to='suspected_untested'（输出改"疑似未覆盖"措辞）。"""
    G = nx.DiGraph()
    G.add_node("test_a", kind="file", source_file="tests/test_a.py")
    for i in range(10):
        G.add_node(f"n{i}", kind="function", source_file=f"mod{i}.py")
    G.add_edge("test_a", "n0")            # 只覆盖 1/10 → 未覆盖率 90% > 30%
    r = untested_symbols(G)
    assert r["gate_failed"] is True and r["degraded_to"] == "suspected_untested"


def test_untested_rejects_undirected_graph():
    """R4-1：生产 serve._load_graph 产 nx.Graph——无向传入必须 TypeError，
    防测试子图可达退化为连通分量遍历（test_x 可达集把整个连通分量误判"已覆盖"）。"""
    import pytest as _pt
    UG = nx.Graph(); UG.add_edge("test_x", "fn")
    with _pt.raises(TypeError):
        untested_symbols(UG)


def test_untested_patterns_configurable():
    """patterns 参数可配置（go/ts 测试约定留配置项不默认启用）：非默认 pattern
    传参时按该 pattern 判定测试文件。"""
    G = nx.DiGraph()
    G.add_node("gofile", kind="file", source_file="foo_test.go")
    G.add_node("gofn", kind="function", source_file="foo.go")
    G.add_edge("gofile", "gofn", relation="imports")
    r = untested_symbols(G, patterns=("*_test.go",))
    assert "gofn" not in r["untested"]    # go 约定 pattern 生效


def test_untested_default_pattern_ignores_non_py_tests():
    """默认 patterns=('test_*.py',)：go 测试文件不命中（go/ts 留配置项不实现）。"""
    G = nx.DiGraph()
    G.add_node("gofile", kind="file", source_file="foo_test.go")
    G.add_node("gofn", kind="function", source_file="foo.go")
    G.add_edge("gofile", "gofn", relation="imports")
    r = untested_symbols(G)   # 默认 patterns
    assert "gofn" in r["untested"]        # go 测试文件不算测试文件 → gofn 未覆盖
