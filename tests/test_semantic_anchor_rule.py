"""semantic 锚定规则：符号级引用拒收（§6.3 Q2，B2 按 id 前缀判定）.

Task 09 换源：validate_semantic_anchors 随 build 步骤从 adapter 迁至 rebuild_entry
（adapter 整体退役，seed 合并由 rebuild_entry 编排）。语义不变。
"""


def _validate(seed):
    from rebuild_entry import validate_semantic_anchors
    return validate_semantic_anchors(seed)


def test_symbol_anchor_rejected():
    seed = {
        "nodes": [
            {"id": "concept:x", "file_type": "concept"},          # semantic，无 kind（真实形态）
            {"id": "function:abc123", "file_type": "code"},      # 符号级--易失
            {"id": "file:src/a.py", "file_type": "code"},         # 文件级--合规
        ],
        "edges": [
            {"source": "concept:x", "target": "function:abc123"},  # 违规（符号锚点）
            {"source": "concept:x", "target": "file:src/a.py"},    # 合规
        ],
    }
    violations = _validate(seed)
    assert len(violations) == 1
    assert "function:abc123" in violations[0]


def test_file_anchor_accepted():
    seed = {
        "nodes": [{"id": "concept:x", "file_type": "concept"}, {"id": "file:src/a.py", "file_type": "code"}],
        "edges": [{"source": "concept:x", "target": "file:src/a.py"}],
    }
    assert _validate(seed) == []


def test_semantic_internal_edge_accepted():
    """semantic -> semantic 内部边不判违规."""
    seed = {
        "nodes": [{"id": "concept:x", "file_type": "concept"}, {"id": "rationale:y", "file_type": "rationale"}],
        "edges": [{"source": "concept:x", "target": "rationale:y"}],
    }
    assert _validate(seed) == []
