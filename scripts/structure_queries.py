"""C1：find_dead_code（Phase 4 Task 12）——入口策略 + 86.4% 闸门降级。

入口双态：
- 应用型 = main 脚本/CLI 入口（__main__.py 文件节点，或 main/__main__ 短名符号）；
- 库型 = __init__.py 公开导出面（re-export 边起点）。
auto（缺省）按 _detect_entry_mode 探测；显式 entry_mode="application"/"library" 时
按同套 per-mode 入口选择直接取锚点（不做探测），R5-6：entry_files 参数已裁决删除
（无测试无实现消费的死参数，显式入口覆盖需求出现时再加）。

R4-1：**G 必须为 nx.DiGraph**——无向图上传入会让 nx.descendants 退化为连通分量遍历，
unreachable 集系统性趋空、>50% 闸门永不触发、find_dead_code 静默失效。防御断言把
生产形态错误（serve._load_graph 产 nx.Graph）变成调用期显式 TypeError，同 Task 9
(_reverse_closure/_fanout_targets) 手法。

R3-2：合并图节点 id 是 hash 形态且无 name 属性——短名唯一事实源是 label，复用
adapter._symbol_short_name（结构查询与 B3 同源，不在本文件重复定义）。

import 推导（按合并图实际边形态实现，见 Task 12 报告）：
- 合并图（codegraph-merged）kind 含 'import' 节点（import 语句本身，仅经 contains 边
  挂在文件节点下，无任何出边）——它们是"文件 import 了什么"的证据，不是代码符号，
  不计入不可达报告、不进符号总数分母；
- 真实使用证据是 relation='imports'（文件节点 → 被导入符号/概念）与 'imports_from'
  （文件 → 文件）两类边——正向闭包沿全部有向边遍历，可达文件节点的 import 目标即
  "被使用"，故 import 边天然作透传证据（无需特判）。
- `::` label（如 BaseTransport::handle_request）不被 _symbol_short_name 的
  .rsplit(".",1) 剥离——入口探测对 `::` 命名 main 有覆盖缺口（Task 9 遗留裁决：不修
  共享 _symbol_short_name，避免影响 B3 fanout 匹配面），本文件记录不修。

>50% 闸门：不可达率 = len(unreachable)/len(symbol nodes) > 0.5 →
gate_failed=True + degraded_to="orphan_hint"（输出改孤儿符号提示措辞，advisory 语义
不变；fork 实测 >50% 是合法降级交付，不是失败）。

C 信封纪律：verdict 由 serve 闭包定 low_confidence——动态分发（runpy/import hook/
反射调用）是静态图天生盲区，本模块不声称确定性。
"""
from __future__ import annotations

from pathlib import Path

import networkx as nx

# 本文件从 adapter import 短名事实源（R3-2 共享）——不在本文件重复定义。
from adapter import _symbol_short_name

# 代码符号 kind 白名单：进入不可达报告与分母（scanned=符号总数）的节点。
# 排除 file（文件不是符号）、import（import 语句是使用证据）、文档/语义概念类
# （(none)/page/heading 等非代码定义）。
_SYMBOL_KINDS = frozenset({
    "function", "method", "class", "variable", "field", "constant",
    "enum", "enum_member", "struct", "trait", "interface", "protocol",
    "namespace", "module", "type_alias", "property", "component",
})


def _application_entries(project_root: Path | None, G: nx.DiGraph) -> set[str]:
    """应用型入口：project_root 给定且存在 __main__.py 时取该文件内节点；
    否则 main/__main__ 短名符号（合并图节点无 name 属性——R3-2 label 末段）。
    实测 fork 合并图 main/__main__ 命中 39 个（含 import 节点，无出边、无害），锚点充足。"""
    if project_root is not None and (project_root / "__main__.py").exists():
        return {n for n, d in G.nodes(data=True)
                if str(d.get("source_file", "")).endswith("__main__.py")}
    mains = {n for n, d in G.nodes(data=True)
             if _symbol_short_name(d, n) in ("main", "__main__")}
    if mains:
        return mains
    return set()


def _library_entries(G: nx.DiGraph) -> set[str]:
    """库型入口 = __init__.py re-export 边起点（公开导出面）。
    匹配双轨：source_file 末段 __init__.py（真实文件），或 kind=='file' 且短名为
    '__init__'（纯图测试态——fixture 无 source_file/label，短名回退 nid；
    限 kind=='file' 防 Python 构造器 __init__ 方法误入入口）。"""
    return {n for n, d in G.nodes(data=True)
            if str(d.get("source_file", "")).endswith("__init__.py")
            or (d.get("kind") == "file" and _symbol_short_name(d, n) == "__init__")}


def _detect_entry_mode(project_root: Path | None, G: nx.DiGraph) -> tuple[str, set[str]]:
    """应用型 = main 脚本/CLI 入口；库型 = __init__ 公开导出面. 返回 (mode, entry_nodes).
    project_root=None（纯图测试态）跳过 __main__.py 文件检查."""
    entries = _application_entries(project_root, G)
    if entries:
        return "application", entries
    return "library", _library_entries(G)


def find_dead_code(G, entry_mode: str = "auto", project_root: Path | None = None) -> dict:
    """静态死代码扫描：入口正向闭包外、不可达的代码符号集 + >50% 闸门降级。

    G 必须为 nx.DiGraph（R4-1：无向 G 上 descendants 退化为连通分量遍历，不可达
    判定失效——生产 _load_graph 产 nx.Graph，须先经 serve._digraph_view / scripts
    ranked.get_digraph 重建有向视图）。

    返回 dict：
      unreachable     不可达符号节点 id 列表（有序，R3-2 短名事实源）
      entry_mode      "application" | "library"
      scanned         符号总数（N1 契约：serve 闭包 scanned=符号总数）
      unreachable_rate 不可达率（round 4 位）
      gate_failed     rate > 0.5
      degraded_to     "orphan_hint"（gate_failed 时）| None
    """
    if not G.is_directed():
        raise TypeError("要求 _digraph_view 产物；无向 G 上 descendants 退化为连通分量遍历，不可达判定失效")
    if entry_mode == "auto":
        mode, entry_nodes = _detect_entry_mode(project_root, G)
    elif entry_mode == "application":
        mode, entry_nodes = "application", _application_entries(project_root, G)
    elif entry_mode == "library":
        mode, entry_nodes = "library", _library_entries(G)
    else:
        raise ValueError(f"Invalid entry_mode {entry_mode!r}; expected 'auto', 'application' or 'library'")

    # 正向闭包：入口 + 全部出向可达（含 import 推导——imports/imports_from 边是使用
    # 证据，可达文件节点的 import 目标即被使用；不需要特判 import 节点本身）。
    reachable: set[str] = set(entry_nodes)
    for e in entry_nodes:
        if e in G:
            reachable |= set(nx.descendants(G, e))

    symbol_nodes = {n for n, d in G.nodes(data=True) if d.get("kind") in _SYMBOL_KINDS}
    unreachable = sorted(symbol_nodes - reachable)
    total = len(symbol_nodes)
    rate = (len(unreachable) / total) if total else 0.0
    gate_failed = rate > 0.5
    return {
        "unreachable": unreachable,
        "entry_mode": mode,
        "scanned": total,
        "unreachable_rate": round(rate, 4),
        "gate_failed": gate_failed,
        "degraded_to": "orphan_hint" if gate_failed else None,
    }
