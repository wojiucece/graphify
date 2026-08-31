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


def _norm_sf(x) -> "str | None":
    """source_file/路径 POSIX 归一化：反斜杠 -> 正斜杠，剥 './' 前缀。"""
    if not x:
        return None
    s = str(x).replace("\\", "/")
    while s.startswith("./"):
        s = s[2:]
    return s or None


def _sf_match(sf, rf) -> bool:
    """seed 节点/边的 source_file 与 refresh 文件 rf 是否同一源文件（upsert 匹配规则）.

    选型说明（refresh 的 rf 是绝对或相对路径，seed 节点 source_file 形态由 extract
    产出决定）：双方 POSIX 归一化后【相等】或【一方为另一方的路径后缀（以 / 为边界，
    防 'mydocs' 误匹配 'docs'）】。覆盖组合：
    - seed 存 root 相对路径（extract #555 in-root 归一化产物，如 'docs/auth.md'）
      x rf 传绝对路径（watch.py 触发面）-> 后缀命中；
    - seed 存 out-of-root portable 裸名（extract #2243 走 root 外分支，如 'notes.md'）
      x rf 绝对路径 -> 后缀命中；
    - 双方同为相对路径 -> 相等命中。
    已知残余风险（接受）：大小写敏感（Windows 大小写差异仅在两侧字符串不同源时触发，
    而两侧均来自同一文件系统的同一路径）；裸名 source_file 会匹配任意目录下同名文件
    （裸名仅在 root 外场景产生，同项目内罕见）。
    """
    a, b = _norm_sf(sf), _norm_sf(rf)
    if not a or not b:
        return False
    return a == b or a.endswith("/" + b) or b.endswith("/" + a)


def run(db_path, output_dir="graphify-out", root=None,
        semantic_seed=None, semantic_refresh=None, wiki=False) -> Path:
    """编排 codegraph 提取 -> build -> cluster -> analyze -> report -> export（方案 §3.1 链路）.

    # CUSTOM: semantic_refresh 的 extract() 缓存锚定 CWD（graphify 既有行为）：hook/watch 场景 CWD=项目根故正确；从任意目录直调 CLI 时 cache 落在 CWD 下 graphify-out/cache/——cache 位置随 CWD 漂移，--output-dir 只管产物不管 cache。
    """
    out = Path(output_dir); out.mkdir(parents=True, exist_ok=True)
    extraction = load_codegraph(db_path)
    # CUSTOM: knowledge_gaps 写 sidecar（最终审查 I1，C4 接线）。load_codegraph 返回的
    # knowledge_gaps（codegraph unresolved_refs status='failed'，adapter 已截断 Top 100）
    # 此前仅存在于返回值零消费——落地 <out>/knowledge-gaps.json 供人工/工具读取消费。
    (out / "knowledge-gaps.json").write_text(
        json.dumps(extraction.get("knowledge_gaps", []), ensure_ascii=False, indent=2),
        encoding="utf-8")
    # CUSTOM: seed 路径解析 + 加载（修复用户实测两轮砖死链）。显式 semantic_seed
    # 参数优先（用它的路径）；否则 <out>/semantic-seed.json——与 rebuild_entry
    # 的 C1 默认发现路径一致（rebuild_entry.py 默认种子发现），两端同路径链路才闭合。
    # 此前 seed 只在显式参数时加载；默认路径仅 rebuild_entry 侧发现，run() 直调
    # （CLI 无 --semantic-seed / 上一轮 refresh 落盘后）会漏拾取。
    seed_path = Path(semantic_seed) if semantic_seed is not None else out / "semantic-seed.json"
    _seed_raw: dict = {}
    seed_nodes: list = []
    seed_edges: list = []
    # semantic_seed 合并（Task 7 补 hyperedges 挂回）
    seed_hyperedges: list = []
    if seed_path.exists():
        _seed_raw = json.loads(seed_path.read_text(encoding="utf-8"))
        # CUSTOM: semantic 锚定校验（最终审查 I1，B2 接线）。semantic 边锚定符号级节点
        # （codegraph kind:hash 形态 id，符号移动行号即变 id）易失——违规仅告警 stderr
        # 不阻断：存量种子可能有合法悬挂锚点，提示后由用户自行决定是否修种子。
        _violations = validate_semantic_anchors(_seed_raw)
        if _violations:
            print(f"CUSTOM: semantic 语义锚定违规 {len(_violations)} 条，首条: {_violations[0]}",
                  file=sys.stderr)
        seed_nodes = list(_seed_raw.get("nodes", []))
        seed_edges = list(_seed_raw.get("edges", []))
        seed_hyperedges = list(_seed_raw.get("hyperedges", []))
    elif semantic_seed is not None:
        # CUSTOM: 显式 seed 路径不存在 -> stderr 警告（用户回归报告：此前旧代码
        # FileNotFoundError 当场 crash，拼错能立刻发现；静默跳过后产出 adapter-only 图
        # 零警告）。保留 fail-loud：警告含传入路径 + 修复提示，不阻断主重建。
        print(f"[run_analysis] 警告: 显式 --semantic-seed 路径不存在: {seed_path}"
              f"——请检查 --semantic-seed 拼写；将按 adapter-only 图继续（无语义面）",
              file=sys.stderr)
    # 默认发现路径（semantic_seed is None，即 <out>/semantic-seed.json 不存在）不打警告：
    # 那是正常首轮场景（无种子从 adapter 冷启动），不是拼错。
    # semantic_refresh（修订方案 §1.4 要求）：对每个 refresh 文件做语义提取，
    # 按 source_file 身份 upsert 进 seed 状态（无 seed 则建）。
    if semantic_refresh:
        from graphify.extract import extract as _extract_semantic
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
            except Exception as e:
                # A: 至少落一条 stderr 日志，避免 extract 失败被静默吞掉（零产出无感知）
                # （sys 用模块顶部导入；此处曾有局部 import sys 使 sys 变为函数局部变量，
                #  导致 run() 前段的 stderr 告警 UnboundLocalError——最终审查 I1 接线时移除）
                print(f"[run_analysis] semantic_refresh 提取失败 {rf}: {e}", file=sys.stderr)
                continue
            # 修复（用户实测复现，两轮沙箱模拟）：refresh 产物此前只合入内存 extraction，
            # 没有任何代码写回 seed 文件（seed 唯一写入点是 split_semantic_seed.py 一次性
            # 迁移工具）——.md 编辑产生 refresh 后，任何不带 refresh 的重建（.py 编辑/
            # SessionEnd hook）产出 adapter-only 图 -> 节点数骤减 -> shrink-guard
            # RuntimeError -> 图永久冻结。
            # upsert 语义（按 source_file 替换，防重复膨胀）：先删除 source_file 与该
            # refresh 文件匹配的旧 seed 节点/边，再插入本次 extract 的新节点/边。
            # 已删源文件的驱逐留给全量重切兜底（既有约定，不实现）。
            seed_nodes = [n for n in seed_nodes if not _sf_match(n.get("source_file"), rf)]
            seed_edges = [e for e in seed_edges if not _sf_match(e.get("source_file"), rf)]
            seed_nodes += r.get("nodes", [])
            seed_edges += r.get("edges", [])
    # 合并：extraction = adapter 产出 + 当前有效 seed 状态（含 refresh upsert 产物）。
    # refresh 不再单独拼进 extraction——统一经 seed 状态合入，避免旧代码"seed 与
    # refresh 各拼一次"造成的同源节点双份（此前仅靠 build 的 id 去重兜底）。
    extraction = {"nodes": extraction["nodes"] + seed_nodes,
                  "edges": extraction["edges"] + seed_edges}
    if semantic_refresh:
        # 落盘（无 seed 则建，时机=refresh 合入 extraction 之后）：把合并后的 seed
        # 状态写回 seed 路径。hyperedges 从旧 seed 原样保留（refresh 不产 hyperedges；
        # _seed_raw 的其余未知键也原样带回）。写盘失败（磁盘/权限）打 stderr 警告
        # 但不阻断主重建——fallback 行为=旧行为（refresh 仅进本轮图），只影响下一轮：
        # 无 refresh 重建仍会 adapter-only 缩量、被 shrink-guard 拦截。
        try:
            _seed_out = dict(_seed_raw)
            _seed_out["nodes"] = seed_nodes
            _seed_out["edges"] = seed_edges
            _seed_out["hyperedges"] = seed_hyperedges
            seed_path.write_text(json.dumps(_seed_out, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
        except Exception as e:
            print(f"[run_analysis] seed 落盘失败 {seed_path}: {type(e).__name__}: {e}"
                  f"——下一轮无 refresh 重建将丢失语义面", file=sys.stderr)
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
    # 从 prev graph.json 重建 G_old（node_link 格式；nx 用模块顶部导入，不再局部重复导入）
    G_old = nx.node_link_graph(prev_data, edges="links" if "links" in prev_data else "edges")
    extraction = load_codegraph(db_path)
    G_new = build_from_json(extraction, root=root)
    # 返回键名：new_nodes/removed_nodes/new_edges/removed_edges/summary（analyze.py:556）
    return _graph_diff(G_old, G_new)


def _parse_refresh(s: str | None) -> list[Path] | None:
    """CLI --semantic-refresh 逗号分隔解析：过滤空段（用户审查 M1，两端一致）.

    rebuild_entry.py 持自包含同名副本（scripts 无包结构，不互相导入）：
    "a.md," 不产生 Path('') -> Path('.')（refresh 指向整个 CWD）。
    None 透传（无 refresh 语义）；"" 返回 []（falsy，下游 if semantic_refresh 等价）。"""
    if s is None:
        return None
    return [Path(p) for p in s.split(",") if p]


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
    # CUSTOM: 逗号分隔 -> 列表并过滤空段（最终审查 M-a；用户审查 M1 抽成 _parse_refresh 两端一致）。
    # 字符串直接传给 run() 会按字符迭代
    refresh = _parse_refresh(args.semantic_refresh)
    print(f"分析完成: {run(args.db_path, args.output_dir, args.root, args.semantic_seed, refresh, args.wiki)}")
