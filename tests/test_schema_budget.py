"""§8 schema 预算：core 工具 schema 总量 ≤ 4000 tok（tiktoken 实测口径）.

budget 是 B 轨道（B1-B3 + C1-C4 描述增强）之后的收尾闸门：工具集每加一个
schema 都要回来量一次，超预算即失败（measured/declared 双口径一致才可信）。
"""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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
