"""单一重建入口：extract(增量) → build → to_json(事实层落盘) → rebuild_fts(FTS 重投影) → 分析。

三个触发面（watch.py 代码事件 / SessionEnd hook / PreCompact hook）都改指本入口。
跨进程互斥用 mkdir 原子锁；完成信号用状态文件（schema v2，graph_fingerprint =
graph.json (mtime_ns, size) 指纹，旧 db_fingerprint 的事实层等价物）。

Task 09 换源（spec §工具面迁移 scripts 重组）：codegraph sync 退役——输入源从 codegraph DB
换成源码语料（extract 增量经 per-file cache，重跑廉价），产物对 = graph.json（事实层）+
.fts-index.db（可重建派生缓存）。锁/stale 接管壳保留；A3 收敛语义诚实平移（重建期间
事实层被并发推进 -> 不再重跑，交 watch/hook 事件流兜底）。状态文件 schema 1 -> 2：
指纹字段换 graph.json 时间戳；旧状态文件（schema 1）的读取器只读 phase/started/
last_duration/git_head 共字段，平滑兼容（不依赖 schema 号）。
"""
from __future__ import annotations
import argparse, json, os, subprocess, sys, tempfile, time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:            # import graphify（本地包，不依赖安装态）
    sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))   # import fts_cache / run_analysis

# 退出码空间（评审 Minor）：EXIT_SYNC_FAIL=4 已随 codegraph sync 退役而无引用——保留
# 常量作退出码契约文档（hook/调用方不应复用 4 作其他语义），勿误删。
EXIT_OK, EXIT_LOCK, EXIT_SYNC_FAIL = 0, 3, 4
EXTRACT_RETRIES, EXTRACT_RETRY_WAIT_S = 3, 2.0   # 提取失败短退避重试（对齐旧 sync 3 次退避）
_STATE_SCHEMA = 2                                  # 状态文件 schema v2（graph_fingerprint 字段）


def _log(msg: str) -> None:
    print(f"[rebuild_entry] {msg}", file=sys.stderr)


def _lock_path(root: Path) -> Path:
    """确定性锁名：路径消毒（tr '/\\:' '___' 语义，沿用 sessionend hook 模式）.
    禁用 hash()：Python 3.3+ 字符串 hash 按 PYTHONHASHSEED 进程随机化，
    跨进程同 root 算出不同值 -> 锁路径不同 -> 互斥失效。"""
    import re
    safe = re.sub(r'[/\\:]', '_', str(root))
    return Path(tempfile.gettempdir()) / f"graphify-rebuild-{safe}.lock"


# E: 锁 stale 阈值。hook 面同步执行 rebuild_entry（分钟级窗口），
# 若进程被强杀 finally 不执行 -> 锁残留 -> 后续三触发面全 exit 3。
# 遇锁时检查年龄超此阈值即接管（清理重建）。
_LOCK_STALE_S = 600  # 10 分钟


def _acquire_lock(root: Path) -> bool:
    """获取 mkdir 原子锁；遇已存在锁时检查年龄，超阈值则接管。返回是否获取。"""
    lock = _lock_path(root)
    try:
        lock.mkdir(parents=True, exist_ok=False)
        (lock / "pid").write_text(str(os.getpid()), encoding="utf-8")
        return True
    except FileExistsError:
        # 检查锁年龄
        try:
            age = time.time() - (lock / "pid").stat().st_mtime
        except OSError:
            age = time.time() - lock.stat().st_mtime
        if age > _LOCK_STALE_S:
            _log(f"锁残留 {age:.0f}s（>{_LOCK_STALE_S}s），接管清理")
            import shutil; shutil.rmtree(lock, ignore_errors=True)
            try:
                lock.mkdir(parents=True, exist_ok=False)
                (lock / "pid").write_text(str(os.getpid()), encoding="utf-8")
                return True
            except FileExistsError:
                return False
        return False


def _state_path(root: Path) -> Path:
    """A1 路径联动总条款：状态文件随 active project root 推导."""
    return root / "graphify-out" / ".rebuild-state.json"


def _write_state(root: Path, lock: Path, payload: dict) -> None:
    """写权单一化：仅锁 owner（pid 即自身）可写；serve 侧只读."""
    try:
        owner = int((lock / "pid").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return
    if owner != os.getpid():
        return
    try:
        _state_path(root).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        _log(f"状态文件写入失败（不阻塞 rebuild）: {e}")


def _finish_state(root: Path, lock: Path, started: float, error: bool = False) -> None:
    """与锁清理同一 finally 块调用；error 路径 phase=error（诚实于 complete）.
    C3/G3：git 可用时载荷加 git_head（rev-parse HEAD，基线锚点）——git 不可用/失败时
    省略字段（schema 只增不改，可缺省；读者侧缺失语义 = "基线未锚定"）。成功/错误路径
    都记（git_head 是仓库事实，与 build 成败无关）。schema v2：complete 载荷不携带
    指纹（以 rebuilding 载荷为准，与 schema 1 相同）；旧状态文件读取器只读共字段。"""
    payload = {"schema": _STATE_SCHEMA, "phase": "error" if error else "complete",
               "started": started, "finished": time.time(),
               "last_duration": round(time.time() - started, 1),
               "project": str(root)}
    gh = _git_head(root)
    if gh is not None:
        payload["git_head"] = gh
    _write_state(root, lock, payload)


def _git_head(root: Path) -> str | None:
    """C3：rev-parse HEAD 全 hash；git 不在 PATH/非 git 仓库/失败 -> None（省略字段语义）.
    R5-1：subprocess 直调（无 shell 管道）；CREATE_NO_WINDOW 防 Windows 弹窗（F5 同款）."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(root),
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           creationflags=flags)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def _read_prev_git_head(root: Path) -> str | None:
    """C3：读上一轮状态文件 git_head（rebuild 覆盖前缓存，变更摘要锚点）.
    git_head 可缺省（G3）——文件缺失/损坏/非 str/非 dict -> None（首次重建无锚点，摘要
    跳过）。Minor-3：合法 JSON 但非 dict（如 []）必须 isinstance(d, dict) 防护——调用点在
    rebuild() 的 try/finally 外，AttributeError 会冒泡且锁未清（stale 锁）；与 serve.py
    侧已捕获 AttributeError 对称。schema v1/v2 均读 git_head 共字段（平滑兼容）。"""
    try:
        d = json.loads(_state_path(root).read_text(encoding="utf-8"))
        gh = d.get("git_head") if isinstance(d, dict) else None
    except (OSError, ValueError, TypeError):
        return None
    return gh if isinstance(gh, str) and gh else None


def _log_changed_summary(root: Path, prev_head: str | None) -> None:
    """C3：rebuild 成功后 stderr 附加一行变更摘要（git diff --name-only <prev>..HEAD）——
    进当时活跃会话上下文。prev_head 不可得（首次）或孤儿 hash（amend/rebase）-> 静默
    （不阻塞 rebuild，stderr 一行日志权限内诚实降级）。"""
    if not prev_head:
        return
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    try:
        r = subprocess.run(["git", "diff", "--name-only", f"{prev_head}..HEAD"],
                           cwd=str(root), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", creationflags=flags)
    except (OSError, subprocess.SubprocessError):
        return
    if r.returncode != 0:
        return   # 孤儿 prev_head -> 无摘要（不告警不阻塞，等下次事件流重建）
    names = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
    if not names:
        return
    shown = ", ".join(names[:10])
    if len(names) > 10:
        shown += f" … +{len(names) - 10} more"
    _log(f"变更摘要（{len(names)} 文件，自 {prev_head[:8]}）：{shown}")


def _read_prev_duration(root: Path) -> float:
    """N2：读上一轮 complete 的 last_duration——rebuilding 载荷覆盖整个文件，
    不继承则时效逃生 max(2*last_duration, 1800) 中的 2x 项恒为 0（设计静默失效）."""
    try:
        d = json.loads(_state_path(root).read_text(encoding="utf-8"))
        if not isinstance(d, dict):
            return 0.0
        return float(d.get("last_duration", 0))
    except (OSError, ValueError, TypeError):
        return 0.0


# === 新链路编排：extract(增量) → build → to_json → rebuild_fts → 分析 ===============
# 事实层指纹复用 fts_cache.fingerprint（graph.json (mtime_ns, size)，缺失 -> None）——
# 状态文件 schema v2 的 graph_fingerprint 字段即其输出（06 票 freshness 指纹平移语义）。

def _detect_code_files(root: Path) -> list[Path]:
    """detect() 语料：code 文件 + 有 AST 提取器的 document 文件（对齐 watch.py
    _rebuild_code 语料口径——markdown 等文档走 AST 提取；语义面由 semantic seed 承载）。
    detect 失败（空目录/无文件）-> 空列表（extract 空输入 -> 空事实层，诚实产出）。"""
    from graphify.detect import detect
    from graphify.extract import _get_extractor
    detected = detect(root)
    files = [Path(f) for f in detected["files"].get("code", [])]
    for doc in detected["files"].get("document", []):
        p = Path(doc)
        if _get_extractor(p) is not None:
            files.append(p)
    return files


def _extract_with_retry(root: Path) -> dict:
    """提取增量（per-file cache 使重跑廉价）+ 失败短退避重试（对齐旧 sync 3 次退避）。
    无代码文件 -> 空 extraction（事实层为空图，不 crash）。extract 失败重试耗尽 -> 抛
    （与旧 sync 失败降级退出同语义：交下个触发面兜底）。"""
    from graphify.extract import extract
    paths = _detect_code_files(root)
    last_e: Exception | None = None
    for _ in range(EXTRACT_RETRIES):
        try:
            if not paths:
                return {"nodes": [], "edges": [], "hyperedges": []}
            # cache_root=root：缓存落 root/graphify-out/cache（不随 CWD 漂移）；root 锚定
            # source_file 相对化/id（与 watch.py _rebuild_code 的 extract 调用同口径）。
            return extract(paths, cache_root=root, root=root)
        except Exception as e:
            last_e = e
            _log(f"extract 失败（{EXTRACT_RETRIES - 1 - _} 次后放弃）: {e}")
            time.sleep(EXTRACT_RETRY_WAIT_S)
    raise last_e if last_e is not None else RuntimeError("extract 失败")


# === 语义种子（从 run_analysis 迁入：rebuild_entry 是 build 编排者）==================
# run_analysis 瘦身为 analysis-only（读事实层 + 生成报告），seed 合并/refresh upsert/
# 锚定校验随 build 步骤迁到本入口。函数与旧 run_analysis 同源（自包含副本，不互相导入）。


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

    双方 POSIX 归一化后【相等】或【一方为另一方的路径后缀（以 / 为边界，防 'mydocs'
    误匹配 'docs'）】。覆盖组合：seed 存 root 相对路径 x rf 绝对路径（watch 触发面）->
    后缀命中；双方同为相对路径 -> 相等命中。已知残余风险（接受）：大小写敏感、裸名
    source_file 会匹配任意目录下同名文件（同项目内罕见）。"""
    a, b = _norm_sf(sf), _norm_sf(rf)
    if not a or not b:
        return False
    return a == b or a.endswith("/" + b) or b.endswith("/" + a)


_SEMANTIC_FILE_TYPES = frozenset({"concept", "document", "rationale", "image"})


def validate_semantic_anchors(seed: dict) -> list[str]:
    """semantic 引用必须锚定文件级节点（§6.3 Q2）.
    B2: 真实种子无 kind，按 id 前缀判定--file: 前缀或 file_type 在语义集为合规锚点；
    符号级 id（原生 kind+hash 形态）判违规（符号移动行号即变 id，易失）。"""
    node_info = {n["id"]: n for n in seed.get("nodes", [])}
    violations = []
    for e in seed.get("edges", []):
        for end in ("source", "target"):
            nid = e.get(end, "")
            n = node_info.get(nid, {})
            # semantic 自身节点不判违规
            if nid.startswith(("concept:", "rationale:", "document:", "image:")):
                continue
            if n.get("_origin") == "semantic" or n.get("file_type") in _SEMANTIC_FILE_TYPES:
                continue
            # 合规锚点：file: 前缀 或 kind=file
            if nid.startswith("file:") or n.get("kind") == "file":
                continue
            # 其余为符号级 -> 违规
            violations.append(f"semantic 边 {end} 锚定符号级节点 {nid}（易失），应锚定文件级节点")
    return violations


def _relativize_failed_refs(extraction: dict, root: Path) -> None:
    """failed_refs.file_path 相对化到 root（原地修改）。

    extract 产出的 failed_refs.file_path 是绝对路径（raw_calls source_file 原样），
    而 graph.json 节点 source_file 是 root 相对形态——07 逐出 identity 匹配
    （watch._reconcile_existing_graph / cli._prune_graph_json_sources）要求同源相对
    posix。watch._relativize_source_files 对 failed_refs 桶同款语义（file_path 字段）；
    无法相对（root 外文件）时保持原样（诚实，不臆造路径）。
    """
    root = Path(root).resolve()
    for fr in extraction.get("failed_refs") or []:
        if not isinstance(fr, dict):
            continue
        fp = fr.get("file_path")
        if not fp:
            continue
        p = Path(fp)
        if not p.is_absolute():
            continue
        try:
            fr["file_path"] = p.resolve().relative_to(root).as_posix()
        except ValueError:
            continue


def _merge_seed(extraction: dict, out: Path, semantic_seed: Path | None,
                semantic_refresh: list[Path] | None, root: Path) -> tuple[dict, list]:
    """把 semantic seed 状态合并进 extraction（从 run_analysis 迁入的 build 步骤）.

    - seed 加载：显式 semantic_seed 优先；默认发现路径 <out>/semantic-seed.json。
      锚定校验违规仅告警 stderr 不阻断（存量种子可能有合法悬挂锚点）。
    - semantic_refresh：对每个 refresh 文件做语义提取，按 source_file 身份 upsert 进
      seed 状态（无 seed 则建），并把合并后的 seed 写回 seed 路径（下一轮无 refresh
      的 rebuild 自动拾取，链路闭合防 shrink-guard 砖死）。
    - **failed_refs 透传（评审 Critical）**：重建 dict 时保留 extract 失败收集器产物
      （知识缺口查询源——knowledge-gaps.json / ranked gap_hit 依赖 graph.json 顶层
      failed_refs）；file_path 相对化到 root（extract 原样绝对路径，07 逐出 identity
      匹配要求相对 posix）。

    返回 (merged_extraction, seed_hyperedges)——hyperedges 由调用方 attach 回图；
    extract 不产 hyperedges（返回键集仅 nodes/edges/failed_refs 等），无丢失面。
    """
    seed_path = Path(semantic_seed) if semantic_seed is not None else out / "semantic-seed.json"
    _seed_raw: dict = {}
    seed_nodes: list = []
    seed_edges: list = []
    seed_hyperedges: list = []
    if seed_path.exists():
        _seed_raw = json.loads(seed_path.read_text(encoding="utf-8"))
        _violations = validate_semantic_anchors(_seed_raw)
        if _violations:
            print(f"[rebuild_entry] semantic 语义锚定违规 {len(_violations)} 条，"
                  f"首条: {_violations[0]}", file=sys.stderr)
        seed_nodes = list(_seed_raw.get("nodes", []))
        seed_edges = list(_seed_raw.get("edges", []))
        seed_hyperedges = list(_seed_raw.get("hyperedges", []))
    elif semantic_seed is not None:
        # 显式 seed 路径不存在 -> stderr 警告（fail-loud 保留，不静默跳过后缩量砖死）
        print(f"[rebuild_entry] 警告: 显式 --semantic-seed 路径不存在: {seed_path}"
              f"——请检查 --semantic-seed 拼写；将按 AST-only 图继续（无语义面）",
              file=sys.stderr)
    if semantic_refresh:
        from graphify.extract import extract as _extract_semantic
        for rf in semantic_refresh:
            try:
                # root 传 Path（与 cache_root 同类型，M4 风格统一）
                r = _extract_semantic([Path(rf)], root=root,
                                      cache_root=root)
                for n in r.get("nodes", []):
                    # refresh 合入的 .md 节点强制标 semantic（extract 产出 _origin='ast'）
                    n["_origin"] = "semantic"
            except Exception as e:
                print(f"[rebuild_entry] semantic_refresh 提取失败 {rf}: {e}",
                      file=sys.stderr)
                continue
            # upsert 语义（按 source_file 替换，防重复膨胀）：先删同源旧 seed 节点/边，
            # 再插入本次 extract 的新节点/边。已删源文件的驱逐留给全量重切兜底。
            seed_nodes = [n for n in seed_nodes if not _sf_match(n.get("source_file"), rf)]
            seed_edges = [e for e in seed_edges if not _sf_match(e.get("source_file"), rf)]
            seed_nodes += r.get("nodes", [])
            seed_edges += r.get("edges", [])
        # 写回 seed 状态（无 seed 则建）。hyperedges 与未知键从旧 seed 原样保留。
        try:
            _seed_out = dict(_seed_raw)
            _seed_out["nodes"] = seed_nodes
            _seed_out["edges"] = seed_edges
            _seed_out["hyperedges"] = seed_hyperedges
            seed_path.write_text(json.dumps(_seed_out, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
        except Exception as e:
            print(f"[rebuild_entry] seed 落盘失败 {seed_path}: {type(e).__name__}: {e}"
                  f"——下一轮无 refresh 重建将丢失语义面", file=sys.stderr)
    # 评审 Critical：failed_refs 透传 + file_path 相对化（extract 失败收集器是知识
    # 缺口唯一事实源，重建 dict 时不可丢——graph.json 顶层是 ranked gap 通道消费点）。
    _relativize_failed_refs(extraction, root)
    extraction = {"nodes": extraction["nodes"] + seed_nodes,
                  "edges": extraction["edges"] + seed_edges,
                  "failed_refs": extraction.get("failed_refs", [])}
    return extraction, seed_hyperedges


def rebuild(project_root: Path, *, out_dir: Path | None = None,
            semantic_seed: Path | None = None, semantic_refresh: list[Path] | None = None,
            skip_sync: bool = False, wiki: bool = False) -> Path:
    root = Path(project_root).resolve()
    out = out_dir or root / "graphify-out"
    # CUSTOM: seed 默认发现（最终审查 C1）。三个自动触发面（watch _trigger_rebuild /
    # SessionEnd / PreCompact hook）都不传 --semantic-seed，迁移后首次自动重建若不带 seed
    # -> 语义面丢失 -> 缩量 -> shrink-guard 拒写 -> 每个触发面都失败，永久砖死。
    # 约定默认种子路径 <out>/semantic-seed.json（split_semantic_seed.py 的 CLI 在 --output
    # 未传时默认写 <graph_json 所在目录>/semantic-seed.json，即此发现路径）。
    # 显式 --semantic-seed 仍优先（仅在 None 时发现）。
    if semantic_seed is None:
        _default_seed = out / "semantic-seed.json"
        if _default_seed.exists():
            semantic_seed = _default_seed
            _log(f"CUSTOM: 发现默认种子 {semantic_seed}，自动合入")
    # mkdir 原子锁（确定性锁名 + stale 检测，跨进程互斥，沿用 sessionend hook 模式）
    if not _acquire_lock(root):
        _log("锁被占用，另一重建正在进行 -> exit 3")
        sys.exit(EXIT_LOCK)
    lock = _lock_path(root)
    prev_git_head = _read_prev_git_head(root)   # C3：覆盖前缓存上一轮 git_head（变更摘要锚点）
    t0 = time.time()
    exc_happened = False
    try:
        # 新链路编排（Task 09 换源）：extract(增量) → build → to_json(事实层落盘) →
        # rebuild_fts(FTS 重投影)。skip_sync 为已废弃 no-op（codegraph sync 已退役，
        # 保留 CLI 兼容 watch.py 既有调用）。
        from fts_cache import rebuild_fts, fingerprint
        extraction = _extract_with_retry(root)
        extraction, seed_hyperedges = _merge_seed(extraction, out, semantic_seed,
                                                  semantic_refresh, root)
        # A1a: rebuilding 标记（幂等）。graph_fingerprint = 重建前事实层指纹 f0（复用
        # fingerprint 一次——不额外 stat；语义与旧 db_fingerprint 的"输入状态标记"对齐）。
        # N2: 继承上轮 last_duration（否则时效逃生 2x 项恒 0，设计静默失效）。
        f0 = fingerprint(out / "graph.json")
        _write_state(root, lock, {
            "schema": _STATE_SCHEMA, "phase": "rebuilding", "started": t0,
            "project": str(root),
            "graph_fingerprint": list(f0) if f0 else None,
            "last_duration": _read_prev_duration(root)})
        from graphify.build import build_from_json
        from graphify.export import to_json, attach_hyperedges
        from graphify.cluster import cluster
        G = build_from_json(extraction, root=root)
        # C2: hyperedges 挂回图（export.py:180）；seed_hyperedges 已由 _merge_seed 备好
        if seed_hyperedges:
            attach_hyperedges(G, seed_hyperedges)
        communities = cluster(G)
        # 事实层落盘（node_link 格式，links 键；serve.py hot-reload 依赖）。
        # B4/审查 fix（Important）：force=True 会绕过 #479 shrink-guard，使缩量
        # （seed/refresh 丢失等）静默覆盖旧图。改 force=False 恢复保护——现有
        # graph.json 节点数 >= 新图时 to_json 返回 False 拒绝写入，据此报错，防止
        # 重复跑进同一 output_dir 时旧图被静默缩量覆盖。
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
                f"[rebuild_entry] shrink-guard 拦截写入 {out / 'graph.json'}："
                f"现有图 {_old_desc}，新图 {G.number_of_nodes()} 个节点，拒绝覆盖"
                f"（#479 防静默缩量）。若确认缩量是数据本身的真实变化，可手动删除 "
                f"{out / 'graph.json'} 后重跑，或临时把 scripts/rebuild_entry.py 中 "
                f"to_json 调用的 force=False 改为 force=True 强制重建。"
            )
        # FTS 重投影（事实层派生缓存，原子替换——删除无损，指纹失效自动重建）
        rebuild_fts(out / "graph.json", out / ".fts-index.db")
        # A3（Task 9 换源）：重建期间事实层被并发推进（我们自己写入后的 f_after 与
        # 分析结束时的指纹不一致 = 其他进程在分析窗口期替换了 graph.json）-> 记录日志
        # 不再重跑，收敛交给 watch/hook 事件流兜底。锁内正常路径不会触发（仅 stale
        # 接管竞态窗口可达）。
        f_after = fingerprint(out / "graph.json")
        # 分析（analysis-only）：报告/百科 + knowledge-gaps（读事实层，不写图）
        from run_analysis import run
        run(out / "graph.json", output_dir=out, root=str(root), wiki=wiki)
        if f_after is not None and fingerprint(out / "graph.json") != f_after:
            _log("重建期间事实层被并发推进，已记录日志；不再重跑，交下个触发面兜底")
        # B4: cache GC 挂载（成功路径末尾、finally 前，锁内）。manifest 锚定
        # mark-and-sweep + 频率门控摊销扫描；GC 失败绝不影响 rebuild 结果。
        # root 显式传项目根（live 重算的 file_hash path-salt 锚点；--out-dir 自定义
        # 时 cache_root.parent 不是项目根）。
        try:
            from cache_gc import gc_cache
            gc_cache(out, out / "manifest.json", root=root)
        except Exception as e:   # GC 失败绝不影响 rebuild 结果
            _log(f"cache gc 异常（忽略）: {e}")
        # C3: rebuild 成功后 stderr 附加一行变更摘要（git diff --name-only <prev>..HEAD，
        # 进当时活跃会话上下文）。prev_head 不可得/孤儿 hash -> 内部静默。
        _log_changed_summary(root, prev_git_head)
        return out
    except BaseException:
        exc_happened = True
        raise
    finally:
        # A1a: 状态收尾必须在锁清理之前（_write_state 读锁 pid 判断 owner；锁没了则拒写）。
        # 写序不变量 E1：run() 落盘 graph.json 先于此处 complete 标记。
        _finish_state(root, lock, t0, error=exc_happened)
        # 误接管防御：若本进程超 _LOCK_STALE_S 被另进程接管，锁目录已含对方的 pid 文件，
        # 非空目录 rmdir 抛 OSError，会掩盖本进程的正常返回或原始异常。包 except 吞掉。
        # 修正（Task 13 E2E 发现）：先删本进程 pid 文件再 rmdir；pid 不匹配（已被接管）
        # 则不碰对方的锁。
        try:
            if (lock / "pid").read_text(encoding="utf-8").strip() == str(os.getpid()):
                (lock / "pid").unlink(missing_ok=True)
                lock.rmdir()
        except OSError:
            pass


def _parse_refresh(s: str | None) -> list[Path] | None:
    """CLI --semantic-refresh 逗号分隔解析：过滤空段（用户审查 M1）。

    "a.md," 此前产生 Path('') -> Path('.')，refresh 指向整个 CWD（与旧 run_analysis.py
    CLI 已有的空段过滤不一致）。None 透传（无 refresh 语义）；"" 返回 []（falsy，
    下游 if semantic_refresh 等价）。"""
    if s is None:
        return None
    return [Path(p) for p in s.split(",") if p]


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="extract(增量) → build → to_json(事实层落盘) → rebuild_fts(FTS 重投影) → 分析（单一重建入口）")
    ap.add_argument("--project", required=True, help="项目根")
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--semantic-seed", default=None)
    ap.add_argument("--semantic-refresh", default=None, help="逗号分隔的语义文件路径")
    ap.add_argument("--skip-sync", action="store_true",
                    help="已废弃 no-op（codegraph sync 已退役；保留供 watch.py 既有调用兼容）")
    ap.add_argument("--wiki", action="store_true", help="生成 Obsidian wiki 出口")
    args = ap.parse_args()
    # CUSTOM: 经 _parse_refresh 过滤空段（M1）："a.md," 不再产生 Path('') -> '.'
    refresh = _parse_refresh(args.semantic_refresh)
    rebuild(args.project,
            out_dir=Path(args.out_dir) if args.out_dir else None,
            semantic_seed=Path(args.semantic_seed) if args.semantic_seed else None,
            semantic_refresh=refresh, skip_sync=args.skip_sync, wiki=args.wiki)
