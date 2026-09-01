"""单一重建入口：codegraph sync -> graphify 分析重建（指纹收敛循环）。

三个触发面（watch.py 代码事件 / SessionEnd hook / PreCompact hook）都改指本入口。
跨进程互斥用 mkdir 原子锁；完成信号用子进程退出码（codegraph 无对外 sync 事件，F1/F5）。"""
from __future__ import annotations
import argparse, json, os, sqlite3, subprocess, sys, tempfile, time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:            # import graphify（本地包，不依赖安装态）
    sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))   # import adapter / run_analysis

EXIT_OK, EXIT_LOCK, EXIT_SYNC_FAIL = 0, 3, 4
SYNC_RETRIES, SYNC_RETRY_WAIT_S = 3, 2.0   # 锁竞争短重试；降级语义对齐移植的 5 次退避


def run_codegraph_sync(root: Path) -> int:
    """子进程跑 codegraph sync。CREATE_NO_WINDOW 防 Windows 弹窗（F5）。"""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    # 小偏差（健壮性）：codegraph 输出 UTF-8（┌◆ 等 box char），text=True 默认按 locale
    # （GBK）解码 -> reader 线程 UnicodeDecodeError 噪声 + 潜在传播崩溃。显式 utf-8 + replace。
    return subprocess.run(
        ["codegraph", "sync", str(root)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=flags).returncode


def _sync_with_retry(root: Path) -> int:
    for _ in range(SYNC_RETRIES):
        code = run_codegraph_sync(root)
        if code == 0:
            return 0
        time.sleep(SYNC_RETRY_WAIT_S)
    return code


def db_fingerprint(db_path: Path) -> tuple[int, int]:
    """(MAX(files.indexed_at), WAL mtime_ns)。indexed_at 捕获写入；
    wal mtime 捕获含删除/纯 checkpoint 的任何写。误报方向是"多重建一轮"，安全（F7）。"""
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        ts = conn.execute("SELECT COALESCE(MAX(indexed_at), 0) FROM files").fetchone()[0]
    finally:
        conn.close()
    wal = db_path.parent / (db_path.name + "-wal")
    return (ts, wal.stat().st_mtime_ns if wal.exists() else 0)


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
    """与锁清理同一 finally 块调用；error 路径 phase=error（诚实于 complete）."""
    _write_state(root, lock, {"schema": 1, "phase": "error" if error else "complete",
                              "started": started, "finished": time.time(),
                              "last_duration": round(time.time() - started, 1),
                              "project": str(root)})


def _read_prev_duration(root: Path) -> float:
    """N2：读上一轮 complete 的 last_duration——rebuilding 载荷覆盖整个文件，
    不继承则时效逃生 max(2*last_duration, 1800) 中的 2x 项恒为 0（设计静默失效）."""
    try:
        return float(json.loads(_state_path(root).read_text(encoding="utf-8"))
                     .get("last_duration", 0))
    except (OSError, ValueError, TypeError):
        return 0.0


def rebuild(project_root: Path, *, db_path: Path | None = None, out_dir: Path | None = None,
            semantic_seed: Path | None = None, semantic_refresh: list[Path] | None = None,
            skip_sync: bool = False) -> Path:
    root = Path(project_root).resolve()
    db = db_path or root / ".codegraph" / "codegraph.db"
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
    t0 = time.time()
    exc_happened = False
    try:
        # db 存在性检查在锁之后（测试 test_lock_held_exits_3 预建锁 -> 应 exit 3 而非 FileNotFoundError）
        if not db.exists():
            raise FileNotFoundError(f"{db} 不存在--先跑 codegraph init")
        for _ in range(2):                                    # 指纹收敛，最多两轮
            if not skip_sync:
                code = _sync_with_retry(root)                 # 非零短退避重试后仍失败 -> 降级退出
                if code != 0:
                    _log(f"sync 重试后仍失败（exit {code}），放弃本轮，交下个触发面兜底")
                    sys.exit(EXIT_SYNC_FAIL)
            f0 = db_fingerprint(db)
            # A1a: rebuilding 标记（每轮重写，幂等）。db_fingerprint 复用 f0——不额外调
            # db_fingerprint（少一次只读查询，且保住 test_rebuild_requeued 的 4 次调用预算）；
            # f0 即本轮收敛基线，语义与重查一致。N2: 继承上轮 last_duration（否则时效逃生
            # 2x 项恒 0，设计静默失效）。写权在锁 owner；serve 只读无写竞态。
            _write_state(root, lock, {
                "schema": 1, "phase": "rebuilding", "started": t0,
                "project": str(root), "db_fingerprint": list(f0),
                "last_duration": _read_prev_duration(root)})
            from run_analysis import run                        # 懒导入（scripts/ 在 sys.path）
            run(db, output_dir=out, root=str(root),
                semantic_seed=semantic_seed, semantic_refresh=semantic_refresh)
            if db_fingerprint(db) == f0:
                return out
            _log("重建期间 DB 变化（daemon 并发写入），再排一轮")
        _log("两轮不收敛，交下个触发面兜底")                    # watch/hook 事件流保证最终触发
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
        # 修正（Task 13 E2E 发现）：brief 原文仅 rmdir 非空目录 -> 恒 OSError 被吞 ->
        # 锁永不释放，每次重建都漏锁，后续触发面 600s 内全 exit 3。改为先删本进程 pid
        # 文件再 rmdir；pid 不匹配（已被接管）则不碰对方的锁。
        try:
            if (lock / "pid").read_text(encoding="utf-8").strip() == str(os.getpid()):
                (lock / "pid").unlink(missing_ok=True)
                lock.rmdir()
        except OSError:
            pass


def _parse_refresh(s: str | None) -> list[Path] | None:
    """CLI --semantic-refresh 逗号分隔解析：过滤空段（用户审查 M1，两端一致）.

    "a.md," 此前产生 Path('') -> Path('.')，refresh 指向整个 CWD（与 run_analysis.py
    CLI 已有的空段过滤不一致）。scripts 无包结构，两文件各持自包含副本，不互相导入。
    None 透传（无 refresh 语义）；"" 返回 []（falsy，下游 if semantic_refresh 等价）。"""
    if s is None:
        return None
    return [Path(p) for p in s.split(",") if p]


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="codegraph sync + graphify 分析重建（单一入口）")
    ap.add_argument("--project", required=True, help="项目根")
    ap.add_argument("--db", default=None, help="codegraph DB 路径（默认 <project>/.codegraph/codegraph.db）")
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--semantic-seed", default=None)
    ap.add_argument("--semantic-refresh", default=None, help="逗号分隔的语义文件路径")
    ap.add_argument("--skip-sync", action="store_true")
    args = ap.parse_args()
    # CUSTOM: 经 _parse_refresh 过滤空段（M1）："a.md," 不再产生 Path('') -> '.'
    refresh = _parse_refresh(args.semantic_refresh)
    rebuild(args.project, db_path=Path(args.db) if args.db else None,
            out_dir=Path(args.out_dir) if args.out_dir else None,
            semantic_seed=Path(args.semantic_seed) if args.semantic_seed else None,
            semantic_refresh=refresh, skip_sync=args.skip_sync)
