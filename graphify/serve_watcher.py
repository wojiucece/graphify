"""serve 内置 watcher：serve 进程内可选开启的文件监听（Task 10）。

把 codegraph watcher 算法（watch.py 已移植的防抖/退避 + 软依赖降级）搬进 serve 进程，
让保存文件后秒级自动更新产物对（graph.json + .fts-index.db）与查询结果：

    文件变化 → 防抖聚合 → extract 增量（含删除处理）→ 全量 build → graph.json 原子落盘
    → FTS 重投影 → serve 缓存进程内直通失效原子换图。

设计坐标：docs/wayfinder/tickets/04-serve-watcher-architecture.md（架构框架 + 三条铁律 +
并发模型）；spec §Watcher。范围纪律：本文件是纯自定义新增，零上游触碰；watch.py 本体不
重构（仅 import 其已移植的防抖/退避常量与批次分类口径）。

关键语义：

- 默认关：serve 侧 --watch / GRAPHIFY_WATCH 显式开启（本模块不含开关）。
- watchdog 软依赖：import 失败降级 mtime 轮询（5s 间隔、os.scandir 非递归栈、轮询发现
  变化直接进常规防抖窗——300ms 快窗在 5s 粒度下无意义跳过）。
- 三条铁律（架构票 04 用户陷阱，全部落进实现）：
  1) 删除语义：维护 pending 删除集；pipeline 从 extraction nodes/edges 剔除
     source_file ∈ 已删除 的条目 + 失效该文件 extract cache + 修剪 semantic-seed 残留；
     MovedEvent 拆为 delete+create；build 前按文件系统实况兜底过滤（防亡灵节点）。
  2) graceful shutdown：stop() 阻塞等待当前批次 pipeline 完成（join + 批次完成信号），
     不许只发信号；graph.json 落盘原子写（tmp + rename，to_json → write_json_atomic）。
  3) 重建期间查询走旧图：pipeline 完成前不动 graph.json（原子替换），完成后经
     on_pipeline_complete 回调进程内直通失效 _GraphContextCache（换图指针原子）。
- 并发模型：watcher 线程内联串行执行 pipeline（防抖窗口天然合并批次）；重建期间 serve
  查询线程继续用旧图。
"""
from __future__ import annotations

import atexit
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path

from graphify.paths import GRAPHIFY_OUT as _GRAPHIFY_OUT

# 防抖/退避常量复用 watch.py 已移植的 codegraph 算法（架构票 04 决策点定案：
# 直接 import，不抽公共模块——同包私有常量，零成本零漂移；抽模块要动 watch.py 本体，
# 违背"不重构 watch.py"的范围纪律）。
from graphify.watch import (
    _MAX_RETRY_BACKOFF,
    _MAX_SYNC_FAILURE_RETRIES,
    _QUICK_SYNC_MAX_PENDING,
    _QUICK_SYNC_QUIET,
    _SEMANTIC_DOC_SUFFIXES,
    _WATCHED_EXTENSIONS,
    _is_ignored,
    _load_graphifyignore,
)

logger = logging.getLogger(__name__)

# watchdog 软依赖：import 失败 -> 降级 mtime 轮询（零新硬依赖）。
try:
    from watchdog.observers import Observer as _WatchdogObserver
    from watchdog.events import FileSystemEventHandler as _FSHandler
except ImportError:  # pragma: no cover - 环境依赖分支（无 watchdog 时降级轮询）
    _WatchdogObserver = None
    _FSHandler = None

# scripts/ 放行 rebuild_entry / fts_cache / run_analysis（fork 自定义面，不在安装包路径）。
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if _SCRIPTS_DIR.is_dir() and str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

DEFAULT_DEBOUNCE = 3.0        # 常规防抖窗（对齐 watch.py --debounce 默认）
DEFAULT_POLL_INTERVAL = 5.0   # 降级轮询间隔（架构票 04 陷阱 2：5s 粒度下 300ms 快窗无意义跳过）
_LOOP_SLEEP = 0.5             # observer 模式下主循环轮询间隔（对齐 watch.py main loop）
_JOIN_TIMEOUT = 60.0          # stop() join 上限（阻塞等待当前批次；异常情况不永久卡死）
_VENDORED_DIRS = frozenset({"node_modules", "__pycache__", ".venv", "venv", "build", "dist", "dist-newstyle"})
_WATCH_ENV_TRUE = frozenset({"1", "true", "yes", "on"})


if _FSHandler is not None:  # pragma: no cover - 依赖 watchdog 是否安装
    class _Handler(_FSHandler):
        """watchdog 事件 → _record（MovedEvent 拆 delete+create，铁律 1）。"""

        def __init__(self, on_event) -> None:
            super().__init__()
            self._on_event = on_event

        def on_created(self, event) -> None:
            self._on_event(Path(event.src_path), deleted=False)

        def on_modified(self, event) -> None:
            self._on_event(Path(event.src_path), deleted=False)

        def on_deleted(self, event) -> None:
            self._on_event(Path(event.src_path), deleted=True)

        def on_moved(self, event) -> None:
            # MovedEvent 拆 delete+create（重命名等价于删旧建新）。
            self._on_event(Path(event.src_path), deleted=True)
            self._on_event(Path(event.dest_path), deleted=False)
else:  # pragma: no cover - 无 watchdog 分支
    class _Handler:  # type: ignore[no-redef]
        def __init__(self, on_event) -> None:
            self._on_event = on_event


class ServeWatcher:
    """serve 进程内文件监听：防抖聚合 → 内联串行 pipeline → 产物对更新 + 缓存直通失效。

    线程模型：watchdog Observer 事件回调线程（或 mtime 轮询主循环）投递变更到
    _changed/_deleted 集；唯一的主循环线程做防抖判定并在防抖窗后【内联】执行 pipeline
    （pipeline 期间新事件继续累积，完成后自然并入下一批次）。pipeline 成功结束触发
    on_pipeline_complete 回调（serve 侧进程内直通失效 _GraphContextCache）。
    """

    def __init__(
        self,
        project_root: "str | Path",
        *,
        out_dir: "str | Path | None" = None,
        debounce: float = DEFAULT_DEBOUNCE,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        on_pipeline_complete: "callable | None" = None,
    ) -> None:
        self._root = Path(project_root).resolve()
        self._out_dir = Path(out_dir).resolve() if out_dir is not None else self._root / _GRAPHIFY_OUT
        self._debounce = debounce
        self._poll_interval = poll_interval
        self._on_complete = on_pipeline_complete
        self._ignore_patterns = _load_graphifyignore(self._root)

        self._observer_mode = _WatchdogObserver is not None and _FSHandler is not None
        self._poll_mode = not self._observer_mode
        self._observer = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._running = False
        self._atexit_registered = False

        self._lock = threading.Lock()
        self._changed: set[Path] = set()
        self._deleted: set[Path] = set()
        self._pending = False
        self._last_trigger = 0.0
        # 降级轮询基线：None = 尚未建立（首次扫描只建基线，不产出 diff）。
        self._prev_snapshot: dict[str, tuple[int, int]] | None = None

    # ── 生命周期 ────────────────────────────────────────────────────────────

    def start(self) -> None:
        """拉起 watcher 线程（非阻塞）。watchdog 启动失败降级轮询（对齐 watch.py 降级）。"""
        if self._running:
            return
        self._stop = threading.Event()
        self._observer = None
        if self._observer_mode:
            handler = _Handler(self._record)
            try:
                observer = _WatchdogObserver()
                observer.schedule(handler, str(self._root), recursive=True)
                observer.start()
            except Exception as exc:
                print(f"[graphify serve watcher] watchdog backend failed to start: {exc}; "
                      f"degrading to mtime polling", file=sys.stderr)
                self._observer_mode = False
            else:
                self._observer = observer
        self._poll_mode = not self._observer_mode
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, name="graphify-serve-watcher",
                                        daemon=True)
        self._thread.start()
        if not self._atexit_registered:
            atexit.register(self.stop)
            self._atexit_registered = True

    def stop(self, *, join_timeout: float | None = None) -> None:
        """阻塞等待当前批次完成（铁律 2：只发信号不等待是禁止的）。

        置停止事件 → 停/join watchdog observer → join 主循环线程。主循环的 pipeline
        内联执行，stop() 返回时当前批次已落盘；退出前还会 flush 一次尚未启动的 pending
        批次（不丢事件）。graph.json 全程原子替换，stop() 返回时事实层文件必然完整。
        """
        if not self._running:
            return
        self._stop.set()
        if self._observer is not None:
            try:
                self._observer.stop()
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=join_timeout or _JOIN_TIMEOUT)
            if self._thread.is_alive():
                logger.warning("serve watcher thread still alive after %.0fs join (daemon thread)",
                               join_timeout or _JOIN_TIMEOUT)
        if self._observer is not None:
            try:
                self._observer.join(timeout=join_timeout or _JOIN_TIMEOUT)
            except Exception:
                pass
        self._running = False

    @property
    def project_root(self) -> str:
        return str(self._root)

    @property
    def backend_name(self) -> str:
        return "watchdog" if self._observer_mode else "polling"

    # ── 事件投递（watchdog handler / 轮询共用）───────────────────────────────

    def _record(self, path: Path, *, deleted: bool) -> None:
        """把单个文件事件投递到变更集（线程安全）。未跟踪路径静默跳过。"""
        p = Path(path)
        if not self._should_track(p):
            return
        with self._lock:
            if deleted:
                self._deleted.add(p)
                self._changed.discard(p)
            else:
                self._changed.add(p)
                self._deleted.discard(p)
            self._pending = True
            self._last_trigger = time.monotonic()

    def _should_track(self, p: Path) -> bool:
        """文件级跟踪判定：扩展名 / graphify-out 排除 / 隐藏路径 / vendored / graphifyignore。

        与 watch.py handler 口径对齐（扩展名 + 隐藏段 + GRAPHIFY_OUT 段）。
        """
        if p.suffix.lower() not in _WATCHED_EXTENSIONS:
            return False
        try:
            rel = p.relative_to(self._root)
        except ValueError:
            return False
        if _GRAPHIFY_OUT in rel.parts:
            return False
        if any(part.startswith(".") for part in rel.parts):
            return False
        if any(part in _VENDORED_DIRS for part in rel.parts[:-1]):
            return False
        if self._ignore_patterns and _is_ignored(p, self._root, self._ignore_patterns):
            return False
        return True

    def _should_traverse_dir(self, d: Path) -> bool:
        """目录级剪枝（轮询遍历用）：graphify-out / 隐藏 / vendored 不进入栈。"""
        if d == self._root:
            return True
        try:
            rel = d.relative_to(self._root)
        except ValueError:
            return False
        if _GRAPHIFY_OUT in rel.parts:
            return False
        if any(part.startswith(".") for part in rel.parts):
            return False
        if rel.parts[-1] in _VENDORED_DIRS:
            return False
        return True

    # ── 降级轮询（mtime 快照 diff）────────────────────────────────────────────

    def _scan_snapshot(self) -> dict[str, tuple[int, int]]:
        """os.scandir 非递归栈遍历，返回 {root 相对 posix: (mtime_ns, size)}。"""
        snap: dict[str, tuple[int, int]] = {}
        stack: list[Path] = [self._root]
        while stack:
            d = stack.pop()
            try:
                with os.scandir(d) as it:
                    for entry in it:
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                child = Path(entry.path)
                                if self._should_traverse_dir(child):
                                    stack.append(child)
                            elif entry.is_file(follow_symlinks=False):
                                child = Path(entry.path)
                                if not self._should_track(child):
                                    continue
                                st = entry.stat(follow_symlinks=False)
                                rel = child.relative_to(self._root).as_posix()
                                snap[rel] = (st.st_mtime_ns, st.st_size)
                        except OSError:
                            continue
            except OSError:
                continue
        return snap

    def _poll_once(self) -> None:
        """轮询一次：与上次快照 diff 产出 changed/deleted（首次只建基线）。"""
        snap = self._scan_snapshot()
        prev = self._prev_snapshot
        self._prev_snapshot = snap
        if prev is None:
            return
        for rel, sig in snap.items():
            if prev.get(rel) != sig:
                self._record(self._root / rel, deleted=False)
        for rel in prev.keys() - snap.keys():
            self._record(self._root / rel, deleted=True)

    # ── 主循环：防抖 + 失败退避 + 内联串行 pipeline ──────────────────────────

    def _run_loop(self) -> None:
        disabled = False
        failure_count = 0
        next_allowed_trigger = 0.0
        try:
            while True:
                time.sleep(self._poll_interval if self._poll_mode else _LOOP_SLEEP)
                if self._stop.is_set():
                    break
                now = time.monotonic()
                if self._poll_mode:
                    self._poll_once()
                with self._lock:
                    pending = self._pending
                    n_changed = len(self._changed)
                    last_trigger = self._last_trigger
                if not pending:
                    continue
                if now < next_allowed_trigger:
                    continue
                # 自适应防抖（复用 watch.py 已移植常量）：≤2 文件快窗仅 observer 模式有效
                # （轮询 5s 粒度下 300ms 快窗无意义，直接走常规窗）。
                effective_debounce = (
                    _QUICK_SYNC_QUIET
                    if (not self._poll_mode and n_changed <= _QUICK_SYNC_MAX_PENDING)
                    else self._debounce
                )
                if (now - last_trigger) < effective_debounce:
                    continue
                changed, deleted = self._take_batch()
                ok = self._flush_batch(changed, deleted)
                if not ok:
                    failure_count += 1
                    backoff = min(self._debounce * 2 ** max(0, failure_count - 1), _MAX_RETRY_BACKOFF)
                    next_allowed_trigger = time.monotonic() + backoff
                    print(f"[graphify serve watcher] rebuild failed (attempt {failure_count}/"
                          f"{_MAX_SYNC_FAILURE_RETRIES}); retrying in {backoff:.1f}s",
                          file=sys.stderr)
                    if failure_count > _MAX_SYNC_FAILURE_RETRIES:
                        print(f"[graphify serve watcher] auto-sync disabled after {failure_count} "
                              f"consecutive failures. Restart serve with --watch to re-enable.",
                              file=sys.stderr)
                        disabled = True
                        break
                    self._restore_batch(changed, deleted)
                else:
                    failure_count = 0
                    next_allowed_trigger = 0.0
        finally:
            # graceful shutdown（铁律 2）：退出前 flush 一次尚未启动的 pending 批次——
            # 保证 stop() join 返回时无未落盘的待办事件（当前批次已完成于循环内）。
            if not disabled:
                changed, deleted = (None, None)
                with self._lock:
                    if self._pending and (self._changed or self._deleted):
                        changed = list(self._changed)
                        deleted = list(self._deleted)
                        self._changed = set()
                        self._deleted = set()
                        self._pending = False
                if changed:
                    try:
                        self._flush_batch(changed, deleted)
                    except Exception as exc:
                        print(f"[graphify serve watcher] final flush failed: {exc}",
                              file=sys.stderr)
            self._running = False

    def _take_batch(self) -> tuple[list[Path], list[Path]]:
        with self._lock:
            changed = list(self._changed)
            deleted = list(self._deleted)
            self._changed = set()
            self._deleted = set()
            self._pending = False
            self._last_trigger = 0.0
            return changed, deleted

    def _restore_batch(self, changed: list[Path], deleted: list[Path]) -> None:
        """失败退避到期后重试同一批（对齐 watch.py pendingFiles 保留语义）。"""
        with self._lock:
            self._changed.update(changed)
            self._deleted.update(deleted)
            self._pending = True

    # ── pipeline（触发链全链路）──────────────────────────────────────────────

    def _flush_batch(self, changed: list[Path], deleted: list[Path]) -> bool:
        """批次分类 + 内联串行 pipeline。返回成功与否（失败由主循环退避/降级）。"""
        semantic_refresh = [
            p for p in changed
            if p.suffix.lower() in _SEMANTIC_DOC_SUFFIXES and p.exists()
        ]
        try:
            self._run_pipeline(changed, deleted, semantic_refresh)
        except Exception as exc:
            print(f"[graphify serve watcher] rebuild failed: {exc}", file=sys.stderr)
            return False
        if self._on_complete is not None:
            try:
                self._on_complete()
            except Exception as exc:
                logger.warning("on_pipeline_complete callback failed: %s", exc)
        return True

    def _run_pipeline(
        self,
        changed: list[Path],
        deleted: list[Path],
        semantic_refresh: list[Path],
    ) -> None:
        """extract(增量, 含删除剔除) → build → 原子落盘 → FTS 重投影（触发链全链路）。

        失败抛异常（由 _flush_batch 捕获转 False 触发退避）。graph.json 落盘原子
        （to_json → write_json_atomic tmp+rename），FTS 重投影原子（rebuild_fts tmp+replace）。
        """
        import rebuild_entry
        from fts_cache import rebuild_fts
        from graphify.build import build_from_json
        from graphify.cluster import cluster
        from graphify.export import attach_hyperedges, to_json

        root = self._root
        out = self._out_dir
        out.mkdir(parents=True, exist_ok=True)
        print(f"[graphify serve watcher] {len(changed)} file(s) changed, "
              f"{len(deleted)} deleted; rebuilding...", file=sys.stderr)
        # 1) extract 增量（per-file cache 使重跑廉价；删除文件不在 detect 语料 -> 天然缺席）
        extraction = rebuild_entry._extract_with_retry(root)
        # 2) 剔除 pending 删除集（兜底：缓存/种子残留的亡灵石，铁律 1）
        if deleted:
            extraction = self._filter_deleted(extraction, deleted)
        # 3) seed 合并（semantic_refresh upsert + 写回）
        extraction, seed_hyperedges = rebuild_entry._merge_seed(
            extraction, out, semantic_seed=None,
            semantic_refresh=semantic_refresh or None, root=root)
        # 4) seed 合并后再次剔除（seed 残留引用已删文件）+ 修剪 seed 文件（防下轮复活）
        if deleted:
            extraction = self._filter_deleted(extraction, deleted)
            self._prune_seed_for_deleted(deleted)
        # 5) 全量 build + 社区检测
        G = build_from_json(extraction, root=root)
        if seed_hyperedges:
            attach_hyperedges(G, seed_hyperedges)
        communities = cluster(G)
        # 6) 事实层原子落盘。删除批次合法缩量 -> force=True 绕 shrink-guard（#479）；
        #    纯修改批次保留 force=False（与 rebuild_entry.rebuild 同语义的保护）。
        ok = to_json(G, communities, str(out / "graph.json"),
                     force=bool(deleted), built_at_commit=None, community_labels={})
        if not ok:
            raise RuntimeError(f"shrink-guard 拒绝写入 {out / 'graph.json'}（见 stderr 详情）")
        # 7) FTS 重投影（05 接口，原子替换）。Windows 上并发只读连接（serve 查询）
        #    可让 os.replace 瞬时 PermissionError——短重试自愈，避免整批退避重跑。
        for _attempt in range(3):
            try:
                rebuild_fts(out / "graph.json", out / ".fts-index.db")
                break
            except PermissionError:
                if _attempt == 2:
                    raise
                time.sleep(0.2)
        # 8) 失效已删文件的 extract cache 条目（卫生；防缓存无限增长/意外复活）
        if deleted:
            self._invalidate_extract_cache(deleted)

    # ── 删除语义实现（铁律 1）────────────────────────────────────────────────

    def _norm_rel(self, p) -> "str | None":
        """source_file / 路径 → root 相对 posix 形态（匹配删除集用）。

        相对路径 resolve() 失败（CWD ≠ root）时保留原始正斜杠形态——删除集侧是事件绝对
        路径（能 resolve 到 root 相对），两侧归一化后相等即匹配。
        """
        if not p:
            return None
        s = str(p).replace("\\", "/")
        while s.startswith("./"):
            s = s[2:]
        try:
            rel = Path(p).resolve().relative_to(self._root.resolve())
            s = rel.as_posix()
        except (ValueError, OSError):
            pass
        return s or None

    def _filter_deleted(self, extraction: dict, deleted: list[Path]) -> dict:
        """剔除 extraction nodes/edges/hyperedges 中 source_file ∈ 已删除 的条目。

        全量 build 前按文件系统实况兜底过滤（架构票铁律 1 防亡灵节点；删后不残留）。
        """
        if not deleted:
            return extraction
        deleted_norm = {self._norm_rel(p) for p in deleted if self._norm_rel(p)}
        if not deleted_norm:
            return extraction
        for bucket in ("nodes", "edges", "hyperedges"):
            items = extraction.get(bucket)
            if not items:
                continue
            extraction[bucket] = [
                item for item in items
                if not (isinstance(item, dict) and self._in_deleted(item.get("source_file"), deleted_norm))
            ]
        return extraction

    def _prune_seed_for_deleted(self, deleted: list[Path]) -> None:
        """从 semantic-seed.json 剔除引用已删文件的节点/边并写回。

        防下轮非 watcher 重建（SessionEnd/PreCompact hook）经 seed 复活亡灵节点。
        """
        seed_path = self._out_dir / "semantic-seed.json"
        if not seed_path.exists():
            return
        try:
            seed = json.loads(seed_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(seed, dict):
            return
        deleted_norm = {self._norm_rel(p) for p in deleted if self._norm_rel(p)}
        if not deleted_norm:
            return
        nodes = seed.get("nodes", [])
        edges = seed.get("edges", [])
        new_nodes = [n for n in nodes if not self._in_deleted(n.get("source_file"), deleted_norm)]
        new_edges = [e for e in edges if not self._in_deleted(e.get("source_file"), deleted_norm)]
        if len(new_nodes) == len(nodes) and len(new_edges) == len(edges):
            return
        seed["nodes"] = new_nodes
        seed["edges"] = new_edges
        try:
            seed_path.write_text(json.dumps(seed, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            logger.warning("semantic-seed prune 写回失败: %s", exc)

    def _invalidate_extract_cache(self, deleted: list[Path]) -> None:
        """失效已删文件的 AST extract cache 条目。

        缓存键是内容 hash（删后无法重算），只能按内嵌 source_file 扫描匹配删除。
        """
        try:
            from graphify.cache import cache_dir
            ast_dir = cache_dir(self._root, kind="ast")
        except Exception as exc:
            logger.warning("extract cache 失效跳过: %s", exc)
            return
        if not ast_dir.is_dir():
            return
        deleted_norm = {self._norm_rel(p) for p in deleted if self._norm_rel(p)}
        if not deleted_norm:
            return
        for f in ast_dir.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            hit = any(
                isinstance(item, dict) and self._in_deleted(item.get("source_file"), deleted_norm)
                for bucket in ("nodes", "edges", "hyperedges", "raw_calls")
                for item in data.get(bucket, [])
            )
            if hit:
                try:
                    f.unlink(missing_ok=True)
                except OSError:
                    pass

    def _in_deleted(self, sf, deleted_norm: set[str]) -> bool:
        return sf is not None and self._norm_rel(sf) in deleted_norm


def mount_watcher(graph_path, ctx_cache, *, watch: "bool | None" = None) -> "ServeWatcher | None":
    """serve 挂载点：读开关（--watch / GRAPHIFY_WATCH）+ 构建 + 启动 watcher。

    graph_path：默认 graph.json 的解析路径；ctx_cache：serve 侧 _GraphContextCache
    实例（pipeline 完成回调直通失效，原子换图）。未开启时返回 None（零副作用）。
    """
    if watch is None:
        watch = os.environ.get("GRAPHIFY_WATCH", "").strip().lower() in _WATCH_ENV_TRUE
    if not watch:
        return None
    resolved = Path(graph_path).resolve()
    watcher = ServeWatcher(
        project_root=str(resolved.parent.parent),
        out_dir=str(resolved.parent),
        on_pipeline_complete=lambda: ctx_cache.invalidate(str(resolved)),
    )
    watcher.start()
    print(f"[graphify serve] watching {watcher.project_root} "
          f"({watcher.backend_name} backend); saves auto-rebuild the graph",
          file=sys.stderr)
    return watcher
