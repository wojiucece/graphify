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
from adapter import load_codegraph, validate_semantic_anchors  # CUSTOM: validate_semantic_anchors 为 B2 接线（最终审查 I1）
from graphify.build import build_from_json
from graphify.cluster import cluster
from graphify.analyze import god_nodes, surprising_connections, find_import_cycles, graph_diff as _graph_diff
from graphify.report import generate as generate_report
from graphify.export import to_json, attach_hyperedges, to_obsidian


def run(db_path, output_dir="graphify-out", root=None,
        semantic_seed=None, semantic_refresh=None, wiki=False) -> Path:
    out = Path(output_dir); out.mkdir(parents=True, exist_ok=True)
    extraction = load_codegraph(db_path)
    # CUSTOM: knowledge_gaps 写 sidecar（最终审查 I1，C4 接线）。load_codegraph 返回的
    # knowledge_gaps（codegraph unresolved_refs status='failed'，adapter 已截断 Top 100）
    # 此前仅存在于返回值零消费——落地 <out>/knowledge-gaps.json 供人工/工具读取消费。
    (out / "knowledge-gaps.json").write_text(
        json.dumps(extraction.get("knowledge_gaps", []), ensure_ascii=False, indent=2),
        encoding="utf-8")
    # semantic_seed 合并（Task 7 补 hyperedges 挂回）
    seed_hyperedges = []
    if semantic_seed is not None:
        seed = json.loads(Path(semantic_seed).read_text(encoding="utf-8"))
        # CUSTOM: semantic 锚定校验（最终审查 I1，B2 接线）。semantic 边锚定符号级节点
        # （codegraph kind:hash 形态 id，符号移动行号即变 id）易失——违规仅告警 stderr
        # 不阻断：存量种子可能有合法悬挂锚点，提示后由用户自行决定是否修种子。
        _violations = validate_semantic_anchors(seed)
        if _violations:
            print(f"CUSTOM: semantic 语义锚定违规 {len(_violations)} 条，首条: {_violations[0]}",
                  file=sys.stderr)
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
                # （sys 用模块顶部导入；此处曾有局部 import sys 使 sys 变为函数局部变量，
                #  导致 run() 前段的 stderr 告警 UnboundLocalError——最终审查 I1 接线时移除）
                print(f"[run_analysis] semantic_refresh 提取失败 {rf}: {e}", file=sys.stderr)
        if sem_nodes or sem_edges:
            extraction = {"nodes": extraction["nodes"] + sem_nodes,
                           "edges": extraction["edges"] + sem_edges}
    G = build_from_json(extraction, root=root)
    # C2: hyperedges 挂回图（export.py:180）；seed_hyperedges 已在 Task 6 seed 合并块备好
    if seed_hyperedges:
        attach_hyperedges(G, seed_hyperedges)
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
    # B4: 经 export.to_json 写 node_link 格式（links 键）。
    # 审查 fix（Important）：force=True 会绕过 #479 shrink-guard，使 DB 侧缩量
    # （seed/refresh 丢失等）静默覆盖旧图。改 force=False 恢复保护——现有
    # graph.json 节点数 >= 新图时 to_json 返回 False 拒绝写入，run() 据此报错，
    # 防止重复跑进同一 output_dir 时旧图被静默缩量覆盖。
    _ok = to_json(G, communities, str(out / "graph.json"), force=False,
                  built_at_commit=None, community_labels={})
    if not _ok:
        from graphify.export import existing_graph_node_count, MALFORMED_GRAPH
        _old_n = existing_graph_node_count(out / "graph.json")
        if _old_n is MALFORMED_GRAPH:
            _old_desc = "无法解析（疑似损坏或写入中途）"
        else:
            _old_desc = f"{_old_n} 个节点"
        raise RuntimeError(
            f"[run_analysis] shrink-guard 拦截写入 {out / 'graph.json'}："
            f"现有图 {_old_desc}，新图 {G.number_of_nodes()} 个节点，拒绝覆盖"
            f"（#479 防静默缩量）。若确认缩量是数据本身的真实变化，可手动删除 "
            f"{out / 'graph.json'} 后重跑，或临时把 scripts/run_analysis.py 中 "
            f"to_json 调用的 force=False 改为 force=True 强制重建。"
        )
    if wiki:
        # C6: to_obsidian(G, communities, output_dir, community_labels=None, cohesion=None)（export.py:681 核读）
        to_obsidian(G, communities, str(out / "wiki"))
    return out


def diff(prev_graph, db_path, root=None) -> dict:
    """上轮 graph.json vs 本次 codegraph 重建的差分（B1 单库原地同步形态）.
    root 必须与产出 prev_graph 的 run() 一致，否则 source_file 归一化不同 -> 假 diff。"""
    prev_data = json.loads(Path(prev_graph).read_text(encoding="utf-8"))
    # 从 prev graph.json 重建 G_old（node_link 格式）
    import networkx as nx
    G_old = nx.node_link_graph(prev_data, edges="links" if "links" in prev_data else "edges")
    extraction = load_codegraph(db_path)
    G_new = build_from_json(extraction, root=root)
    # 返回键名：new_nodes/removed_nodes/new_edges/removed_edges/summary（analyze.py:556）
    return _graph_diff(G_old, G_new)


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
    # CUSTOM: 逗号分隔 -> 列表并过滤空段（最终审查 M-a）。字符串直接传给 run() 会按字符迭代
    refresh = [p for p in args.semantic_refresh.split(",") if p] if args.semantic_refresh else None
    print(f"分析完成: {run(args.db_path, args.output_dir, args.root, args.semantic_seed, refresh, args.wiki)}")
