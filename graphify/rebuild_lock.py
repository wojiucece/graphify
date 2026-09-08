"""跨进程互斥原语：mkdir 原子锁（确定性锁名 + pid 文件 + 600s stale 接管）.

per-project-watcher 01 票从 scripts/rebuild_entry.py 逐字提升——锁算法单一事实源，
rebuild_entry 反向 import 本模块（scripts → graphify 本就是允许方向），serve 侧
watcher（后续票）共用同一原语，防多触发面并发重建互踩 extract cache。
落点选独立模块而非 serve_watcher.py：rebuild_entry import serve_watcher 会引入
"重建入口依赖 serve 侧模块"的怪耦合，还连带 watchdog 软依赖 import。

- 确定性锁名：路径消毒（tr '/\\:' '___' 语义，沿用 sessionend hook 模式）。禁用
  hash()：Python 3.3+ 字符串 hash 按 PYTHONHASHSEED 进程随机化，跨进程同 root 算出
  不同值 -> 锁路径不同 -> 互斥失效。
- pid 文件：锁目录内写当前 pid（owner 判定 + stale 年龄锚点）。
- stale 接管：遇锁查 pid 文件年龄，超阈值（进程强杀 finally 不执行的残留）即
  清理重建（防三触发面永久 exit 3）。

零 watchdog/serve_watcher 依赖：独立 import 本模块不得拉起 serve 侧模块
（watchdog 是 serve_watcher 的软依赖）。
"""
from __future__ import annotations
import os, sys, tempfile, time
from pathlib import Path


def _log(msg: str) -> None:
    print(f"[rebuild_lock] {msg}", file=sys.stderr)


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
