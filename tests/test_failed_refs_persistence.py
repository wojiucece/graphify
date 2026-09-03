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
