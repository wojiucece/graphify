"""analysis-only 编排器：读事实层 graph.json + 生成报告（Task 09 换源瘦身）。

Task 09 换源（spec §工具面迁移 scripts 重组）：adapter 依赖整体删除——输入不再是
codegraph DB，而是 graph.json 唯一事实层（rebuild_entry 新链路 extract → build →
to_json 落盘产物）。本模块只做分析（cluster/analyze/report/wiki + knowledge-gaps
sidecar），不写 graph.json（写图是 rebuild_entry 的 to_json 步骤）。

knowledge-gaps.json：从 graph.json 顶层 failed_refs（Task 04 raw_calls 失败收集器，
Task 07 持久化）派生——{from_node, callee_name, line, file_path} → {ref, node, file,
line}，替代旧 adapter 的 unresolved_refs status='failed' 源。
"""
from __future__ import annotations
import json, sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))            # import graphify（本地包）

import networkx as nx
from graphify.build import build_from_json
from graphify.cluster import cluster
from graphify.analyze import god_nodes, surprising_connections, find_import_cycles, graph_diff as _graph_diff
from graphify.report import generate as generate_report
from graphify.export import to_obsidian


def _read_graph(graph_path: str | Path) -> dict:
    """graph.json 加载（node_link 格式；links/edges 双键兼容）。缺失/损坏 -> 抛
    （事实层有问题，分析无从建——诚实暴露，不静默降级）。"""
    p = Path(graph_path)
    return json.loads(p.read_text(encoding="utf-8"))


def _knowledge_gaps(graph_data: dict) -> list[dict]:
    """failed_refs（Task 04 失败收集器）→ knowledge-gaps sidecar 载荷.
    映射：{from_node, callee_name, line, file_path} → {ref, node, file, line}（与旧
    adapter unresolved_refs 输出同构，spec Q2 gap 通道换源）。缺失/非 list -> []。"""
    failed = graph_data.get("failed_refs")
    if not isinstance(failed, list):
        return []
    out = []
    for fr in failed:
        if not isinstance(fr, dict):
            continue
        ref = fr.get("callee_name")
        if not ref:
            continue
        out.append({"ref": ref,
                    "node": fr.get("from_node") or "",
                    "file": fr.get("file_path") or "",
                    "line": fr.get("line") or 0})
    return out


def run(graph_path, output_dir="graphify-out", root=None, wiki=False) -> Path:
    """读事实层 graph.json -> cluster/analyze -> GRAPH_REPORT.md (+ wiki/knowledge-gaps).

    graph_path: 事实层 graph.json（rebuild_entry 新链路产物）。
    root: source_file 相对化锚点（与产出 graph.json 的 rebuild 一致，防报告 path 漂移）。
    wiki: 生成 Obsidian wiki 出口（to_obsidian，export.py:681）。
    """
    out = Path(output_dir); out.mkdir(parents=True, exist_ok=True)
    graph_data = _read_graph(graph_path)
    # CUSTOM: knowledge-gaps 写 sidecar（spec Q2 gap 通道换源）。failed_refs 随
    # graph.json 事实层落盘（Task 07 持久化），本处派生 {ref,node,file,line} 形态
    # 供人工/工具读取消费（与旧 adapter knowledge_gaps 输出同构）。
    (out / "knowledge-gaps.json").write_text(
        json.dumps(_knowledge_gaps(graph_data), ensure_ascii=False, indent=2),
        encoding="utf-8")
    # 契约入口（build_from_json 处理 node_link 格式：links 键 remap + hyperedges 折叠 +
    # 旧字段别名，与 rebuild_entry 的 build 步骤同源——读取即得同一 G）。
    G = build_from_json(graph_data, root=root)
    communities = cluster(G)
    god_list = god_nodes(G)
    surprise_list = surprising_connections(G, communities=communities)  # 恒传 communities（Task 12 锁死路径）
    cycles = find_import_cycles(G)
    # 契约调整（Step 4）：report.generate 在无 warning 分支硬访问
    # detection_result['total_files']/['total_words']（report.py:137），
    # 仅传 {"import_cycles": ...} 会 KeyError。补全事实层可得的 total_files（去重
    # source_file），total_words 无来源置 0。
    _src_files = {n.get("source_file") for n in graph_data.get("nodes", [])
                  if isinstance(n, dict) and n.get("source_file")}
    report = generate_report(
        G, communities=communities, cohesion_scores={}, community_labels={},
        god_node_list=god_list, surprise_list=surprise_list,
        detection_result={"import_cycles": cycles,
                          "total_files": len(_src_files), "total_words": 0,
                          "needs_graph": True, "warning": None},
        token_cost={"total": 0},
        root=root or str(Path(graph_path).resolve().parent))
    (out / "GRAPH_REPORT.md").write_text(report, encoding="utf-8")
    if wiki:
        # C6: to_obsidian(G, communities, output_dir, community_labels=None, cohesion=None)（export.py:681 核读）
        to_obsidian(G, communities, str(out / "wiki"))
    return out


def diff(prev_graph, current_graph, root=None) -> dict:
    """上轮 graph.json vs 当前 graph.json 的差分（B1 双图形态，Task 09 换源）.
    root 必须与产出两张图的 rebuild 一致，否则 source_file 归一化不同 -> 假 diff。"""
    prev_data = _read_graph(prev_graph)
    G_old = build_from_json(prev_data, root=root)
    cur_data = _read_graph(current_graph)
    G_new = build_from_json(cur_data, root=root)
    # 返回键名：new_nodes/removed_nodes/new_edges/removed_edges/summary（analyze.py:556）
    return _graph_diff(G_old, G_new)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="读事实层 graph.json + 生成报告（analysis-only）")
    ap.add_argument("graph_path", help="事实层 graph.json 路径")
    ap.add_argument("--output-dir", default="graphify-out")
    ap.add_argument("--root", default=None)
    ap.add_argument("--wiki", action="store_true")
    args = ap.parse_args()
    print(f"分析完成: {run(args.graph_path, args.output_dir, args.root, args.wiki)}")
