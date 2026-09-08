"""graphify/rebuild_lock.py 单元测试（per-project-watcher 01 票锁原语提升）.

锁算法自 scripts/rebuild_entry.py 逐字提升后的模块级直测：消毒禁 hash（跨进程
PYTHONHASHSEED 钉死）+ mkdir 原子互斥 + pid 文件 + 600s stale 接管 + 零 watchdog
软依赖。rebuild_entry 侧的锁回归线（互斥/stale 接管/exit 3 契约）仍在
tests/test_rebuild_entry.py，原样保留作零回归验收。
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import graphify.rebuild_lock as rl

_REPO = Path(__file__).resolve().parent.parent


def _cleanup(root: Path) -> None:
    """测试收尾：清锁目录（ignore_errors，防失败用例残留污染后续运行）."""
    shutil.rmtree(rl._lock_path(root), ignore_errors=True)


# === 锁名消毒（禁 hash()）========================================================

def test_lock_path_sanitized_and_deterministic(tmp_path):
    """确定性锁名：同 root 同路径；消毒字符 /\\: 不出现在锁名；落系统临时目录。"""
    root = tmp_path.resolve()
    lock1, lock2 = rl._lock_path(root), rl._lock_path(root)
    assert lock1 == lock2, "同 root 两次算锁名必须一致（确定性，禁 hash()）"
    assert lock1.parent == Path(tempfile.gettempdir()), f"锁应落系统临时目录: {lock1}"
    for ch in ("/", "\\", ":"):
        assert ch not in lock1.name, f"消毒字符 {ch!r} 泄入锁名: {lock1.name}"
    assert lock1.name.startswith("graphify-rebuild-") and lock1.name.endswith(".lock")


def test_lock_path_same_across_pythonhashseeds(tmp_path):
    """禁 hash() 回归线：两个不同 PYTHONHASHSEED 的进程对同 root 算出同一锁名。

    hash() 随机化一旦回潮（消毒被改成 hash(str(root))），种子 1/2 进程锁路径分裂，
    跨进程互斥失效——本测试钉死消毒必须纯字面替换。"""
    root = tmp_path.resolve()
    code = ("import sys\n"
            "from pathlib import Path\n"
            "from graphify.rebuild_lock import _lock_path\n"
            "print(_lock_path(Path(sys.argv[1])))\n")
    outs = []
    for seed in ("1", "2"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        r = subprocess.run([sys.executable, "-c", code, str(root)],
                           cwd=str(_REPO), env=env, capture_output=True,
                           text=True, encoding="utf-8", timeout=120)
        assert r.returncode == 0, f"PYTHONHASHSEED={seed} 进程失败: {r.stderr}"
        outs.append(r.stdout.strip())
    assert outs[0] == outs[1], f"跨 PYTHONHASHSEED 锁名漂移（hash 回潮）: {outs}"
    assert outs[0] == str(rl._lock_path(root)), \
        f"子进程锁名与主进程不一致: {outs[0]} != {rl._lock_path(root)}"


# === mkdir 原子互斥 + pid 文件 ===================================================

def test_mutex_second_acquire_rejected(tmp_path):
    """mkdir 原子性：已持锁时二次 acquire 拒绝（跨进程互斥的同语义近似），
    锁目录内 pid 文件 = 当前进程 pid。"""
    root = tmp_path.resolve()
    try:
        assert rl._acquire_lock(root) is True
        lock = rl._lock_path(root)
        assert (lock / "pid").read_text(encoding="utf-8") == str(os.getpid())
        assert rl._acquire_lock(root) is False, "已持锁时二次 acquire 必须失败"
    finally:
        _cleanup(root)


def test_fresh_lock_not_taken_over(tmp_path):
    """新鲜锁（pid 年龄 < 600s）：不接管，acquire 拒绝，对方锁原样保留。"""
    root = tmp_path.resolve()
    lock = rl._lock_path(root)
    lock.mkdir(parents=True)
    (lock / "pid").write_text("99999", encoding="utf-8")
    try:
        assert rl._acquire_lock(root) is False
        assert (lock / "pid").read_text(encoding="utf-8") == "99999", "新鲜锁被误清理"
    finally:
        _cleanup(root)


# === stale 接管 ==================================================================

def test_stale_lock_taken_over(tmp_path):
    """E: 残留锁（pid 年龄 > 600s，进程强杀 finally 不执行的残留）被接管清理，
    owner 换成当前进程（防三触发面永久拿不到锁）。"""
    root = tmp_path.resolve()
    lock = rl._lock_path(root)
    lock.mkdir(parents=True)
    (lock / "pid").write_text("99999", encoding="utf-8")
    old = time.time() - 660
    os.utime(lock / "pid", (old, old))
    try:
        assert rl._acquire_lock(root) is True, "超阈 stale 锁必须被接管"
        assert (lock / "pid").read_text(encoding="utf-8") == str(os.getpid()), \
            "接管后 owner 应换成当前进程"
    finally:
        _cleanup(root)


# === 依赖纪律 / 单一事实源 =======================================================

def test_import_does_not_pull_watchdog_or_serve_watcher():
    """零 watchdog 软依赖：独立 import graphify.rebuild_lock 不得加载
    serve_watcher/watchdog（验收清单——本模块不许连带 serve 侧 import）。"""
    code = ("import sys\n"
            "import graphify.rebuild_lock\n"
            "assert 'watchdog' not in sys.modules, 'watchdog 被拉起'\n"
            "assert 'graphify.serve_watcher' not in sys.modules, 'serve_watcher 被拉起'\n")
    r = subprocess.run([sys.executable, "-c", code], cwd=str(_REPO),
                       capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert r.returncode == 0, f"独立 import 触发 serve 侧加载: {r.stderr}"


def test_rebuild_entry_uses_lifted_module():
    """单一事实源钉死：rebuild_entry 的锁原语就是 graphify.rebuild_lock 的同一对象
    （本地副本回潮/双副本分叉在此显式失败）。"""
    import rebuild_entry
    assert rebuild_entry._lock_path is rl._lock_path
    assert rebuild_entry._acquire_lock is rl._acquire_lock
    assert rebuild_entry._LOCK_STALE_S == rl._LOCK_STALE_S == 600
