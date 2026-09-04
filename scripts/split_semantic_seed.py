"""旧 graph.json -> semantic 种子拆分（方案 §4.3）.
A1: 边读 links 键（node_link 格式，兼容旧 edges）；C2: hyperedges 顶层整体携带；C8: 旧格式（无 _origin）拒绝作 seed，提示先升级."""
from __future__ import annotations
import json, sys
from pathlib import Path
from typing import Any

_SEMANTIC_ORIGIN = "semantic"

# C8（控制器裁决，覆盖方案 §4.1 的 file_type 启发式）：真实旧图
# （BusinessAnalysis 根/quant_project 等 2026-06/07 构建）无 _origin 键。
# 仅靠 file_type 判别不可靠（document/rationale 与代码节点无硬区分边界），
# 且旧图 id 空间与 codegraph 适配器不兼容——故直接拒绝作 seed，
# 返回空 seed + 警告，要求先跑 graphify update 升级图再迁移。
_LEGACY_REJECT_WARNING = "旧格式无 _origin，拒绝作 seed，请先跑 graphify update 升级图再迁移"


def split(graph_json_path, output_path=None):
    data = json.loads(Path(graph_json_path).read_text(encoding="utf-8"))
    nodes = data.get("nodes", [])
    # A1: links 键优先，兼容旧 edges
    edges = data.get("links") or data.get("edges", [])
    hyperedges = data.get("hyperedges", [])  # C2 顶层
    has_origin = any("_origin" in n for n in nodes)

    # C8（控制器裁决）：旧格式图无 _origin 标记，拒绝作 seed。
    # 不产出空种子文件（下游 --semantic-seed 会静默合入空 nodes/edges，
    # 掩盖"未迁移"事实）——仅返回空 seed + 警告。
    if not has_origin:
        return {"seed": {"nodes": [], "edges": [], "hyperedges": []},
                "semantic_nodes": 0, "seed_edges": 0,
                "legacy_format": True, "warning": _LEGACY_REJECT_WARNING}

    sem_ids = {n["id"] for n in nodes if n.get("_origin") == _SEMANTIC_ORIGIN}
    anchor_ids = set()
    for e in edges:
        if e["source"] in sem_ids and e["target"] not in sem_ids: anchor_ids.add(e["target"])
        if e["target"] in sem_ids and e["source"] not in sem_ids: anchor_ids.add(e["source"])
    carry_ids = sem_ids | anchor_ids
    seed_nodes = [n for n in nodes if n["id"] in carry_ids]
    seed_edges = [e for e in edges if e["source"] in sem_ids or e["target"] in sem_ids]

    # Task 11 收尾：退役 --codegraph-db 锚点改指。原实现把旧图锚点 id（file:src/app.py
    # 形态）按 source_file 匹配改指为 codegraph 文件节点 id（file:app.py）；codegraph
    # 运行时已退役，无 DB 可对——且新链路文件节点 id 是 path 式（graphify_security_
    # sanitize_label），改指目标本身已不存在。存量旧图 seed 迁移若锚点 id 与目标图不
    # 一致，由迁移方在目标图上做 id 映射（超出本脚本范围）。

    seed = {"nodes": seed_nodes, "edges": seed_edges, "hyperedges": hyperedges}
    stats = {"seed": seed, "semantic_nodes": len(sem_ids), "seed_edges": len(seed_edges),
             "legacy_format": not has_origin,
             "warning": None}
    if output_path is not None:
        Path(output_path).write_text(json.dumps(seed, ensure_ascii=False, indent=2), encoding="utf-8")
    return stats


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="旧 graph.json -> semantic 种子")
    ap.add_argument("graph_json")
    ap.add_argument("--output", default=None)
    args = ap.parse_args()
    # CUSTOM: --output 未传时默认写 <graph_json 所在目录>/semantic-seed.json（最终审查 C1）
    # ——这是 rebuild_entry.rebuild() 的默认发现路径（<out>/semantic-seed.json，out 默认
    # <root>/graphify-out）。迁移时拆分一条命令落位，watch/hook 自动重建即可拾取种子。
    output = args.output
    if output is None:
        output = Path(args.graph_json).resolve().parent / "semantic-seed.json"
    s = split(args.graph_json, output)
    print(f"semantic 节点 {s['semantic_nodes']}/边 {s['seed_edges']}/hyperedges {len(s['seed']['hyperedges'])}"
          + (f"/{s['warning']}" if s.get('warning') else "")
          + f" -> 种子已写入 {output}")
