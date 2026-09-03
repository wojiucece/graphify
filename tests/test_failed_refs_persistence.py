"""07 票 Seam 2（产物对）：failed_refs 持久化接线——信号跨构建边界存活。

覆盖三条链路：
- build.py build_from_json / build / build_merge / merge_raw_extraction：failed_refs
  从 extraction dict 挂到 G.graph / new dict（增量并集既有+新，重提的以新的为准）。
- export.py to_json：G.graph["failed_refs"] 提升到 graph.json 顶层（与 --no-cluster
  直写同落点，ranked gap 通道消费顶层键）。
- graphify/watch.py _reconcile_existing_graph：增量重建保留既有失败引用（evict 删除/
  重提源），全量重建只用新提取的。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import networkx as nx

from graphify.build import (
    build as build_multi,
    build_from_json,
    build_merge,
    merge_raw_extraction,
)
from graphify.export import to_json


def _failed_refs(n=2, prefix="ref") -> list[dict]:
    return [
        {"from_node": f"{prefix}_caller{i}", "callee_name": f"{prefix}{i}",
         "line": i, "file_path": f"pkg/{prefix}{i}.py"}
        for i in range(n)
    ]


# ── build_from_json / build：从 extraction dict 挂到 G.graph ──────────────────

def test_build_from_json_carries_failed_refs_to_graph_attr():
    ext = {"nodes": [{"id": "n1", "label": "f()", "source_location": "L1:C1"}],
           "edges": [], "hyperedges": [], "failed_refs": _failed_refs(1)}
    G = build_from_json(ext)
    fr = G.graph.get("failed_refs")
    assert fr and fr[0]["callee_name"] == "ref0"


def test_build_from_json_skips_empty_failed_refs():
    """空集不挂键（无缺口则图不带 failed_refs，避免 schema 噪音）。"""
    G = build_from_json({"nodes": [], "edges": [], "hyperedges": []})
    assert "failed_refs" not in G.graph


def test_build_unions_failed_refs_across_chunks():
    chunks = [
        {"nodes": [], "edges": [], "hyperedges": [], "failed_refs": _failed_refs(2, "a")},
        {"nodes": [], "edges": [], "hyperedges": [], "failed_refs": _failed_refs(2, "b")},
    ]
    G = build_multi(chunks, dedup=False)
    names = {fr["callee_name"] for fr in G.graph.get("failed_refs", [])}
    assert names == {"a0", "a1", "b0", "b1"}


# ── to_json：failed_refs 提升到 graph.json 顶层 ───────────────────────────────

def test_to_json_hoists_failed_refs_to_top_level(tmp_path):
    G = build_from_json(
        {"nodes": [{"id": "n1", "label": "f()", "source_location": "L1:C1"}],
         "edges": [], "hyperedges": [], "failed_refs": _failed_refs(1)})
    out = tmp_path / "graph.json"
    to_json(G, {}, str(out), force=True)
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data.get("failed_refs") and data["failed_refs"][0]["callee_name"] == "ref0"
    assert "failed_refs" not in data.get("graph", {})  # 提升后 graph 槽不再残留


# ── merge_raw_extraction / build_merge：增量并集既有 + 新 ────────────────────

def test_merge_raw_extraction_carries_existing_failed_refs(tmp_path):
    out = tmp_path / "graph.json"
    existing = {"nodes": [], "edges": [], "hyperedges": [], "failed_refs": _failed_refs(1, "old")}
    out.write_text(json.dumps(existing), encoding="utf-8")
    new = {"nodes": [], "edges": [], "hyperedges": [], "failed_refs": _failed_refs(1, "new")}
    merged = merge_raw_extraction(new, out)
    names = {fr["callee_name"] for fr in merged.get("failed_refs", [])}
    assert names == {"old0", "new0"}  # 并集：既有保留 + 新提取加入


def test_merge_raw_extraction_dedupes_reemitted(tmp_path):
    out = tmp_path / "graph.json"
    same = _failed_refs(1, "dup")
    out.write_text(json.dumps({"nodes": [], "edges": [], "hyperedges": [],
                               "failed_refs": same}), encoding="utf-8")
    merged = merge_raw_extraction(
        {"nodes": [], "edges": [], "hyperedges": [], "failed_refs": same}, out)
    # 按四元组去重：重提的以新的为准（新在前，旧同键被丢弃）
    assert len(merged.get("failed_refs", [])) == 1


def test_build_merge_carries_existing_failed_refs(tmp_path):
    out = tmp_path / "graph.json"
    out.write_text(json.dumps(
        {"nodes": [{"id": "n1", "label": "f()", "source_location": "L1:C1"}],
         "edges": [], "hyperedges": [],
         "failed_refs": _failed_refs(1, "old")}), encoding="utf-8")
    G = build_merge([{"nodes": [], "edges": [], "hyperedges": [],
                      "failed_refs": _failed_refs(1, "new")}],
                    graph_path=out, dedup=False)
    names = {fr["callee_name"] for fr in G.graph.get("failed_refs", [])}
    assert names == {"old0", "new0"}


# ── 07 票评审 I1/I3：三引擎共享逐出语义（删除/重提源 failed_refs 不永生）────────
# 与 watch._reconcile_existing_graph 同源：file_path 命中重提源（new_sources）或
# 剪除源（prune/deleted）→ 逐出；unchanged 源 → 保留。

def test_build_merge_evicts_failed_refs_for_rebuilt_and_deleted(tmp_path):
    out = tmp_path / "graph.json"
    out.write_text(json.dumps(
        {"nodes": [
            {"id": "n_u", "label": "u()", "source_file": "pkg/unchanged.py",
             "source_location": "L1:C1"},
            {"id": "n_r", "label": "r()", "source_file": "pkg/re_extracted.py",
             "source_location": "L1:C1"},
            {"id": "n_d", "label": "d()", "source_file": "pkg/deleted.py",
             "source_location": "L1:C1"},
        ], "edges": [], "hyperedges": [],
         "failed_refs": [
             {"from_node": "n_u", "callee_name": "u_missing", "line": 1,
              "file_path": "pkg/unchanged.py"},
             {"from_node": "n_r", "callee_name": "stale_callee", "line": 1,
              "file_path": "pkg/re_extracted.py"},
             {"from_node": "n_d", "callee_name": "deleted_callee", "line": 1,
              "file_path": "pkg/deleted.py"},
         ]}), encoding="utf-8")
    G = build_merge(
        [{"nodes": [{"id": "n_r", "label": "r()", "source_file": "pkg/re_extracted.py",
                     "source_location": "L1:C1"}],
          "edges": [], "hyperedges": [],
          "failed_refs": [
              {"from_node": "n_r", "callee_name": "fresh_callee", "line": 2,
               "file_path": "pkg/re_extracted.py"},
          ]}],
        graph_path=out, prune_sources=["pkg/deleted.py"], dedup=False)
    names = {fr["callee_name"] for fr in G.graph.get("failed_refs", [])}
    assert names == {"u_missing", "fresh_callee"}  # stale 重提源逐出 + deleted 逐出


def test_merge_raw_extraction_evicts_failed_refs_for_rebuilt_and_deleted(tmp_path):
    out = tmp_path / "graph.json"
    out.write_text(json.dumps(
        {"nodes": [
            {"id": "n_u", "label": "u()", "source_file": "pkg/unchanged.py",
             "source_location": "L1:C1"},
            {"id": "n_r", "label": "r()", "source_file": "pkg/re_extracted.py",
             "source_location": "L1:C1"},
            {"id": "n_d", "label": "d()", "source_file": "pkg/deleted.py",
             "source_location": "L1:C1"},
        ], "edges": [], "hyperedges": [],
         "failed_refs": [
             {"from_node": "n_u", "callee_name": "u_missing", "line": 1,
              "file_path": "pkg/unchanged.py"},
             {"from_node": "n_r", "callee_name": "stale_callee", "line": 1,
              "file_path": "pkg/re_extracted.py"},
             {"from_node": "n_d", "callee_name": "deleted_callee", "line": 1,
              "file_path": "pkg/deleted.py"},
         ]}), encoding="utf-8")
    new = {"nodes": [{"id": "n_r", "label": "r()", "source_file": "pkg/re_extracted.py",
                      "source_location": "L1:C1"}],
           "edges": [], "hyperedges": [],
           "failed_refs": [
               {"from_node": "n_r", "callee_name": "fresh_callee", "line": 2,
                "file_path": "pkg/re_extracted.py"},
           ]}
    merged = merge_raw_extraction(new, out, prune_sources=["pkg/deleted.py"])
    names = {fr["callee_name"] for fr in merged.get("failed_refs", [])}
    assert names == {"u_missing", "fresh_callee"}


def test_prune_graph_json_sources_evicts_failed_refs(tmp_path):
    """07 票评审 I1：_prune_graph_json_sources（--no-cluster 增量早退剪源路径）同步
    逐出被剪源（删除/排除）的 failed_refs——与节点/边同一逐出语义。"""
    from graphify.cli import _prune_graph_json_sources
    graph_path = tmp_path / "graph.json"
    graph_path.write_text(json.dumps(
        {"nodes": [
            {"id": "n1", "label": "a()", "source_file": "pkg/keep.py",
             "source_location": "L1:C1"},
            {"id": "n2", "label": "b()", "source_file": "pkg/gone.py",
             "source_location": "L1:C1"},
        ], "edges": [], "hyperedges": [],
         "failed_refs": [
             {"from_node": "n1", "callee_name": "keep_missing", "line": 1,
              "file_path": "pkg/keep.py"},
             {"from_node": "n2", "callee_name": "gone_missing", "line": 1,
              "file_path": "pkg/gone.py"},
         ]}), encoding="utf-8")
    n_removed = _prune_graph_json_sources(graph_path, ["pkg/gone.py"])
    assert n_removed == 1
    data = json.loads(graph_path.read_text(encoding="utf-8"))
    names = {fr["callee_name"] for fr in data.get("failed_refs", [])}
    assert names == {"keep_missing"}  # gone.py 的失败引用被逐出


def test_watch_reconcile_evicts_failed_refs_for_rebuilt_and_deleted(tmp_path):
    """07 票评审 I3：watch._reconcile_existing_graph 的 failed_refs 逐出（file_path
    identity 命中删除/重提源 → 逐出；unchanged 源 → 保留）。"""
    from graphify.watch import _reconcile_existing_graph
    root = tmp_path
    (root / "pkg").mkdir()
    for name in ("unchanged.py", "re_extracted.py"):
        (root / "pkg" / name).write_text("x = 1\n", encoding="utf-8")
    out_dir = root / "graphify-out"
    out_dir.mkdir()
    existing_path = out_dir / "graph.json"
    existing_path.write_text(json.dumps(
        {"nodes": [
            {"id": "n_u", "label": "u()", "source_file": "pkg/unchanged.py",
             "source_location": "L1:C1"},
            {"id": "n_r", "label": "r()", "source_file": "pkg/re_extracted.py",
             "source_location": "L1:C1"},
            {"id": "n_d", "label": "d()", "source_file": "pkg/deleted.py",
             "source_location": "L1:C1"},
        ], "links": [], "hyperedges": [],
         "failed_refs": [
             {"from_node": "n_u", "callee_name": "u_missing", "line": 1,
              "file_path": "pkg/unchanged.py"},
             {"from_node": "n_r", "callee_name": "stale_callee", "line": 1,
              "file_path": "pkg/re_extracted.py"},
             {"from_node": "n_d", "callee_name": "deleted_callee", "line": 1,
              "file_path": "pkg/deleted.py"},
         ]}), encoding="utf-8")
    result = {
        "nodes": [{"id": "n_r", "label": "r()", "source_file": "pkg/re_extracted.py",
                   "source_location": "L1:C1"}],
        "edges": [], "hyperedges": [],
        "failed_refs": [
            {"from_node": "n_r", "callee_name": "fresh_callee", "line": 2,
             "file_path": "pkg/re_extracted.py"},
        ],
    }
    deleted_identity = (root / "pkg" / "deleted.py").resolve().as_posix()
    merged, _ = _reconcile_existing_graph(
        existing_path, result,
        out=out_dir, project_root=root, watch_root=root,
        code_files=[root / "pkg" / "unchanged.py", root / "pkg" / "re_extracted.py"],
        extract_targets=[root / "pkg" / "re_extracted.py"],
        full_rebuild=False, deleted_paths=set(),
        deleted_source_identities={deleted_identity},
    )
    names = {fr["callee_name"] for fr in merged.get("failed_refs", [])}
    assert names == {"u_missing", "fresh_callee"}
