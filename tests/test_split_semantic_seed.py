"""split_semantic_seed.py 测试（A1 links 键 + C2 hyperedges + C8 旧格式）."""
import json
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"
SRC = FIXTURES / "graph-legacy" / "semantic-seed-source.json"


def test_split_reads_links_not_edges():
    """A1: 真实旧图边在 links 键."""
    from split_semantic_seed import split
    stats = split(SRC, output_path=None)
    assert stats["seed_edges"] == 3  # fixture 有 3 条 semantic links


def test_split_carries_hyperedges():
    """C2: 顶层 hyperedges 整体携带."""
    from split_semantic_seed import split
    stats = split(SRC, output_path=None)
    assert len(stats["seed"]["hyperedges"]) == 1
    assert stats["seed"]["hyperedges"][0]["id"] == "auth_cluster"


def test_split_carries_semantic_nodes():
    from split_semantic_seed import split
    stats = split(SRC, output_path=None)
    ids = {n["id"] for n in stats["seed"]["nodes"]}
    assert "concept:auth" in ids and "rationale:why-jwt" in ids
    assert "file:docs/auth.md" in ids  # 锚点文件节点携带


def test_split_writes_output(tmp_path):
    from split_semantic_seed import split
    out = tmp_path / "seed.json"
    split(SRC, output_path=out)
    data = json.loads(out.read_text(encoding="utf-8"))
    assert "nodes" in data and "edges" in data and "hyperedges" in data


def test_split_carries_anchors_as_is_without_codegraph_remap(tmp_path):
    """Task 11 收尾：--codegraph-db 锚点改指退役——无 codegraph DB 可对，锚点按原样携带.
    原 B6 场景（file:src/app.py 改指 codegraph file:app.py）随 codegraph 退役消失；
    新链路文件节点 id 是 path 式，锚点 id 是否需映射由迁移方在目标图上决定。"""
    from split_semantic_seed import split
    old = {
        "nodes": [
            {"id": "file:src/app.py", "label": "app.py", "source_file": "src/app.py", "_origin": "ast", "file_type": "code"},
            {"id": "concept:x", "label": "X", "_origin": "semantic", "file_type": "concept"},
        ],
        "links": [{"source": "concept:x", "target": "file:src/app.py", "relation": "documents", "_origin": "semantic"}],
        "hyperedges": [],
    }
    old_path = tmp_path / "old.json"
    old_path.write_text(json.dumps(old), encoding="utf-8")
    stats = split(old_path, output_path=None)
    seed = stats["seed"]
    ids = {n["id"] for n in seed["nodes"]}
    # 锚点文件节点按原样携带（不做任何 id 改指）
    assert "file:src/app.py" in ids
    assert seed["edges"][0]["target"] == "file:src/app.py"


def test_split_tolerates_null_source_file(tmp_path):
    """旧图 source_file=null（JSON null -> None）节点：semantic 节点正常收进 seed，
    不抛异常（退役后无锚点改指循环，null 天然容忍——保留测试防回归携带）。"""
    from split_semantic_seed import split
    old = {
        "nodes": [
            # 两个 source_file=null 节点：semantic 概念 + 被边锚到的 ast 文件
            {"id": "concept:nullsrc", "label": "N", "_origin": "semantic", "file_type": "concept", "source_file": None},
            {"id": "file:src/app.py", "label": "app.py", "source_file": None, "_origin": "ast", "file_type": "code"},
        ],
        "links": [{"source": "concept:nullsrc", "target": "file:src/app.py", "relation": "documents", "_origin": "semantic"}],
        "hyperedges": [],
    }
    old_path = tmp_path / "old-null.json"
    old_path.write_text(json.dumps(old), encoding="utf-8")
    stats = split(old_path, output_path=None)
    seed = stats["seed"]
    ids = {n["id"] for n in seed["nodes"]}
    assert "concept:nullsrc" in ids  # semantic 节点正常收进 seed
    assert "file:src/app.py" in ids  # 锚点文件节点正常携带
    assert stats["semantic_nodes"] == 1


def test_split_rejects_legacy_no_origin(tmp_path):
    """C8: 旧格式（无 _origin 键）拒绝作 seed——空 seed + 警告 + 不落盘.
    锁定控制器裁决：无 _origin 时不再继续拆（file_type 启发式已废弃），
    未来若有人回退到按 file_type 继续拆，本测试立即红."""
    from split_semantic_seed import split
    legacy = {
        "nodes": [{"id": "x", "label": "X", "file_type": "concept"}],
        "links": [],
        "hyperedges": [],
    }
    old_path = tmp_path / "legacy.json"
    old_path.write_text(json.dumps(legacy), encoding="utf-8")
    out = tmp_path / "should-not-exist.json"
    stats = split(old_path, output_path=out)
    seed = stats["seed"]
    assert seed["nodes"] == [] and seed["edges"] == [] and seed["hyperedges"] == []
    assert stats["legacy_format"] is True
    assert stats["warning"]  # 非空：拒绝原因提示
    assert not out.exists()  # 拒绝路径不产出空种子文件
