"""§8 schema 预算：core 工具 schema 总量 ≤ 4000 tok（tiktoken 实测口径）.

budget 是 B 轨道（B1-B3 + C1-C4 描述增强）之后的收尾闸门：工具集每加一个
schema 都要回来量一次，超预算即失败（measured/declared 双口径一致才可信）。
"""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 预期 15 工具名集合（SDD Minor-5）：唯一事实源 _TOOL_SPECS 应精确覆盖此集——
# 防将来有人在 list_tools() 里加字面量 types.Tool（绕过 _TOOL_SPECS）逃过预算断言。
_EXPECTED_TOOL_NAMES = frozenset({
    "query_graph", "get_node", "get_neighbors", "get_community", "god_nodes",
    "graph_stats", "shortest_path", "get_ranked_context", "get_changed_symbols",
    "get_hotspots", "find_dead_code", "get_untested_symbols",
    "list_prs", "get_pr_impact", "triage_prs",
})

def test_schema_budget_under_4000():
    from graphify import serve
    schemas = serve._all_tool_schemas()   # 新辅助：从 _build_server 的 list_tools 提取纯 schema dict
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        total = sum(len(enc.encode(json.dumps(s, ensure_ascii=False))) for s in schemas)
        measured = True
    except ImportError:
        total = sum(len(json.dumps(s, ensure_ascii=False)) // 4 for s in schemas)
        measured = False
    assert total <= 4000, f"schema 总量 {total} tok 超 4000 预算（measured={measured}）"

def test_all_tool_schemas_covers_expected_tool_set():
    """§8 Minor-5：工具名集合 == 预期 15 工具——list_tools 加字面量工具逃不过预算断言."""
    from graphify import serve
    names = {s["name"] for s in serve._all_tool_schemas()}
    assert names == _EXPECTED_TOOL_NAMES, (
        f"工具名集合漂移：多出 {names - _EXPECTED_TOOL_NAMES} / 缺失 "
        f"{_EXPECTED_TOOL_NAMES - names}——新工具必须进 _TOOL_SPECS（含进预算）")
