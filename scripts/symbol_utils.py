"""scripts 层共享符号小工具（Task 09 从 adapter.py 迁出——adapter 整体退役）。

B3 R3-2 合并图短名：graphify 原生 id 是 kind+hash 形态且节点无 name 属性——短名唯一
事实源是 label。adapter 退役后本函数成为 scripts 层的共享来源（structure_queries.py
消费）；serve.py 持自包含副本（graphify 包不反向依赖 scripts/），防漂移锁定靠
tests/test_dispatch_trace.py 一致性测试。
"""


def _symbol_short_name(d: dict, nid: str) -> str:
    """B3 R3-2 合并图短名：label 首 token 末段（消歧后缀 ' (N)' 剥离）。
    合并图 id 是 hash 形态且无 name 属性——短名唯一事实源是 label；label 缺失回退 nid。"""
    return str(d.get("label") or nid).split(" ")[0].rsplit(".", 1)[-1]
