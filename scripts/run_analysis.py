"""codegraph 驱动的 graphify 分析编排器（方案 §3.1 链路）.
产物经 export.to_json（node_link 格式 + shrink-guard + dangling 修剪），serve.py hot-reload 依赖。"""
from __future__ import annotations
import json, sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))            # import graphify（本地包）
sys.path.insert(0, str(Path(__file__).resolve().parent))  # import adapter

import networkx as nx
from adapter import load_codegraph
from graphify.build import build_from_json
from graphify.cluster import cluster
from graphify.analyze import god_nodes, surprising_connections, find_import_cycles
from graphify.report import generate as generate_report
from graphify.export import to_json, attach_hyperedges, to_obsidian


def run(db_path, output_dir="graphify-out", root=None,
        semantic_seed=None, semantic_refresh=None, wiki=False) -> Path:
    out = Path(output_dir); out.mkdir(parents=True, exist_ok=True)
    extraction = load_codegraph(db_path)
    # semantic_seed 合并（Task 7 补 hyperedges 挂回）
    seed_hyperedges = []
    if semantic_seed is not None:
        seed = json.loads(Path(semantic_seed).read_text(encoding="utf-8"))
        extraction = {"nodes": extraction["nodes"] + seed.get("nodes", []),
                       "edges": extraction["edges"] + seed.get("edges", [])}
        seed_hyperedges = seed.get("hyperedges", [])
    # semantic_refresh（修订方案 §1.4 要求）：对每个 refresh 文件做语义提取，
    # 按 source_file 身份 upsert 进 extraction（无 seed 则建）。
    if semantic_refresh:
        from graphify.extract import extract as _extract_semantic
        sem_nodes, sem_edges = [], []
        for rf in semantic_refresh:
            try:
                # A: extract() 签名是 extract(paths: list[Path], *, root=None, ...)，
                # 传单个 Path 会 TypeError: 'WindowsPath' object is not iterable
                r = _extract_semantic([Path(rf)], root=Path(root) if root else None)
                for n in r.get("nodes", []):
                    # B: extract 产出的 .md 节点自带 _origin='ast'，setdefault 不覆盖->
                    # refresh 合入的 .md 仍标 ast；split 按 _origin=='semantic' 判别会漏收。
                    # 强制赋值为 semantic，使 refresh 的语义节点进种子链路。
                    n["_origin"] = "semantic"
                sem_nodes += r.get("nodes", [])
                sem_edges += r.get("edges", [])
            except Exception as e:
                # A: 至少落一条 stderr 日志，避免 extract 失败被静默吞掉（零产出无感知）
                import sys
                print(f"[run_analysis] semantic_refresh 提取失败 {rf}: {e}", file=sys.stderr)
        if sem_nodes or sem_edges:
            extraction = {"nodes": extraction["nodes"] + sem_nodes,
                           "edges": extraction["edges"] + sem_edges}
    G = build_from_json(extraction, root=root)
    communities = cluster(G)
    god_list = god_nodes(G)
    surprise_list = surprising_connections(G, communities=communities)  # 恒传 communities（Task 12 锁死路径）
    cycles = find_import_cycles(G)
    # 契约调整（Step 4）：report.generate 在无 warning 分支硬访问
    # detection_result['total_files']/['total_words']（report.py:137），
    # 仅传 {"import_cycles": ...} 会 KeyError。补全 codegraph 侧可得的
    # total_files（去重 source_file），total_words 无来源置 0。
    _src_files = {n.get("source_file") for n in extraction["nodes"] if n.get("source_file")}
    report = generate_report(
        G, communities=communities, cohesion_scores={}, community_labels={},
        god_node_list=god_list, surprise_list=surprise_list,
        detection_result={"import_cycles": cycles,
                          "total_files": len(_src_files), "total_words": 0,
                          "needs_graph": True, "warning": None},
        token_cost={"total": 0},
        root=root or str(Path(db_path).resolve().parent))
    (out / "GRAPH_REPORT.md").write_text(report, encoding="utf-8")
    # B4: 经 export.to_json 写 node_link 格式（links 键 + backup_if_protected + prune_dangling）
    to_json(G, communities, str(out / "graph.json"), force=True,
            built_at_commit=None, community_labels={})
    if wiki:
        # C6: to_obsidian(G, communities, output_dir, community_labels=None, cohesion=None)（export.py:681 核读）
        to_obsidian(G, communities, str(out / "wiki"))
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="codegraph 驱动 graphify 分析")
    ap.add_argument("db_path")
    ap.add_argument("--output-dir", default="graphify-out")
    ap.add_argument("--root", default=None)
    ap.add_argument("--semantic-seed", default=None)
    ap.add_argument("--semantic-refresh", default=None)
    ap.add_argument("--wiki", action="store_true")
    args = ap.parse_args()
    print(f"分析完成: {run(args.db_path, args.output_dir, args.root, args.semantic_seed, args.semantic_refresh, args.wiki)}")
