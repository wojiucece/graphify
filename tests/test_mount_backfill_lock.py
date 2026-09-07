"""per-project-watcher Task 04：挂载即无条件补齐 + 跨进程互斥接线。

spec 测试决策：
- 只测外部行为（图/状态文件内容、重建标记），不断言 registry 内部队列结构；
- E2E 走真实管线（真实 extract→build→FTS，无 mock）；
- 跨进程互斥用真实 mkdir 锁双持有方竞争（rebuild_lock 单一事实源）；
- 锁目录卫生复用 test_rebuild_lock.py 的 _cleanup 模式（防残留污染用例）。

覆盖（任务验收清单）：
- 主断言 4：proj-b 图预先弄陈旧 → 查询挂 watcher → 无条件补齐重建后 graph.json 反映新语料
- 每挂载周期至多入队一次：alive 反复查询不重复补齐；dead 替换 = 新挂载周期（再补一次）
- 默认项目 eager mount 不触发补齐（显式断言）
- 跨进程互斥双方向：watcher 持锁期间 hook rebuild_entry → exit 3；hook 持锁期间 watcher
  拿锁失败 restore 待重试、不丢批次、不持闸（释放信号量后重试）
- 收敛语义：hook 重建结果收敛后 watcher 重试时指纹命中 → 跳过实际重建（不重复 build）
- 补齐走全局信号量排队，不阻塞查询响应（查询返回时补齐仍 rebuilding，freshness 诚实标注）
- 挂载补齐与挂载后即时编辑无缝衔接：并入同批次（不双跑）
- Task 10 全套既有测试零回归（tests/test_serve_watcher.py 独立验证）
"""
import json
import shutil
import threading
import time
from pathlib import Path

import pytest

import graphify.rebuild_lock as rl


def _mini_proj(tmp_path: Path) -> Path:
    """mini 项目（2 个 Python 文件）；返回 resolve() 归一 root（锁名跨进程确定性）。"""
    root = Path(tmp_path).resolve()
    root.mkdir(parents=True, exist_ok=True)
    (root / "a.py").write_text(
        "import b\n\ndef foo():\n    return b.bar()\n", encoding="utf-8")
    (root / "b.py").write_text("def bar():\n    return 1\n", encoding="utf-8")
    return root


def _wait_for(pred, timeout: float = 30.0, interval: float = 0.1) -> bool:
    """轮询直至 pred() 为真。容忍 watcher 线程并发原子替换 graph.json 的瞬时
    Windows 文件锁（PermissionError ⊂ OSError）——Task 10 _wait_for_fts_hit 的
    重试探针模式平移，避免与重建管线赛跑读半写/锁文件。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if pred():
                return True
        except OSError:
            pass  # 瞬时锁窗口，重试
        time.sleep(interval)
    try:
        return bool(pred())
    except OSError:
        return False


def _labels(out_dir: Path) -> set:
    """读取 out_dir/graph.json 的节点 label 集合（外部行为断言用）。"""
    g = out_dir / "graph.json"
    if not g.exists():
        return set()
    data = json.loads(g.read_text(encoding="utf-8"))
    return {n["label"] for n in data.get("nodes", [])}


def _cleanup(root: Path) -> None:
    """锁目录卫生（test_rebuild_lock._cleanup 同款）：删锁目录，防残留污染后续用例。"""
    shutil.rmtree(rl._lock_path(root), ignore_errors=True)


class _FakeCache:
    """registry 的 ctx_cache 接缝：on_pipeline_complete → invalidate 记录。"""

    def __init__(self):
        self.invalidated = []

    def invalidate(self, path):
        self.invalidated.append(path)


@pytest.fixture
def polling(monkeypatch):
    """强制降级轮询（无 watchdog 语义，可确定性测试防抖/信号量/停机）。"""
    import graphify.serve_watcher as W
    monkeypatch.setattr(W, "_WatchdogObserver", None)
    monkeypatch.setattr(W, "_FSHandler", None)


@pytest.fixture
def fast_watch(monkeypatch):
    """serve 构建前注入短防抖/快轮询（registry 构造时读模块常量，需先 monkeypatch）。"""
    import graphify.serve_watcher as W
    monkeypatch.setattr(W, "DEFAULT_DEBOUNCE", 0.1)
    monkeypatch.setattr(W, "DEFAULT_POLL_INTERVAL", 0.2)


# === 验收 1：主断言 4——惰性挂载无条件补齐 ======================================

def test_lazy_mount_backfills_stale_graph(polling, fast_watch, tmp_path):
    """主断言 4：proj-b 图预先弄陈旧（改语料不重建）→ 查询 proj-b 挂 watcher →
    无条件补齐重建后 graph.json 反映新语料（修复挂载前的历史陈旧）。"""
    import graphify.serve as S
    import rebuild_entry
    root = _mini_proj(tmp_path / "proj-a")
    proj_b = _mini_proj(tmp_path / "proj-b")
    rebuild_entry.rebuild(root)
    rebuild_entry.rebuild(proj_b)
    # 弄陈旧：改 proj-b 语料不重建（graph.json 仍是 v1）
    (proj_b / "a.py").write_text(
        "import b\n\ndef foo():\n    return b.bar()\n\ndef backfill_sym():\n    return 42\n",
        encoding="utf-8")
    assert "backfill_sym()" not in _labels(proj_b / "graphify-out"), "前置：图未含新语料"
    server = S._build_server(str(root / "graphify-out" / "graph.json"), watch=True)
    registry = server._graphify_registry
    try:
        assert registry.get(proj_b) is None, "前置：proj-b 未挂载"
        server._graphify_select_graph(str(proj_b))  # 查询 proj-b → 惰性挂载 + 无条件补齐
        wb = registry.get(proj_b)
        assert wb is not None and wb.is_alive, "查询 proj-b 后 watcher 未挂载"
        assert _wait_for(lambda: "backfill_sym()" in _labels(proj_b / "graphify-out")), \
            "挂载补齐未重建出新语料符号"
        # 默认项目（proj-a）图不受 proj-b 补齐影响
        assert "backfill_sym()" not in _labels(root / "graphify-out"), "proj-a 图被污染"
    finally:
        registry.stop_all()


# === 验收 2：每挂载周期至多入队一次 ==============================================

def test_backfill_once_per_mount_cycle_dead_replace(polling, tmp_path, monkeypatch):
    """每挂载周期至多入队一次：alive 反复查询不重复补齐；dead 替换 = 新挂载周期再补一次。"""
    import graphify.serve_watcher as W
    import rebuild_entry
    root = _mini_proj(tmp_path)
    rebuild_entry.rebuild(root)
    (root / "a.py").write_text(
        "import b\n\ndef foo():\n    return b.bar()\n\ndef cycle_sym():\n    return 1\n",
        encoding="utf-8")
    assert "cycle_sym()" not in _labels(root / "graphify-out"), "前置：图未含新语料"
    monkeypatch.setattr(W, "_MAX_SYNC_FAILURE_RETRIES", 2)
    registry = W.WatcherRegistry(_FakeCache(), debounce=0.1, poll_interval=0.2)
    w1 = registry.mount(root, root / "graphify-out")
    calls1 = {"n": 0}
    real1 = w1._run_pipeline

    def rec1(changed, deleted, sr):
        calls1["n"] += 1
        return real1(changed, deleted, sr)
    w1._run_pipeline = rec1
    try:
        # 首次挂载 → 无条件补齐（1 次全量重建收敛到新语料）
        assert _wait_for(lambda: "cycle_sym()" in _labels(root / "graphify-out"), timeout=40), \
            "首挂补齐未完成"
        assert calls1["n"] >= 1, "首挂未触发补齐"
        # alive 反复查询不重复补齐（幂等挂载，不重建）
        n_after = calls1["n"]
        w_same = registry.mount(root, root / "graphify-out")
        assert w_same is w1, "alive 查询应幂等复用"
        time.sleep(0.6)  # 跨多个轮询周期
        assert calls1["n"] == n_after, f"alive 反复查询触发了重复补齐: {calls1}"
        # dead 替换 = 新挂载周期 → 再补一次
        def boom(changed, deleted, sr):
            raise RuntimeError("boom")
        w1._run_pipeline = boom
        (root / "a.py").write_text("x = 1\n", encoding="utf-8")
        assert _wait_for(lambda: not w1.is_alive, timeout=40), "watcher 未自禁用"
        w2 = registry.mount(root, root / "graphify-out")
        assert w2 is not w1 and w2.is_alive, "dead 替换未产生全新 watcher"
        calls2 = {"n": 0}
        real2 = w2._run_pipeline

        def rec2(changed, deleted, sr):
            calls2["n"] += 1
            return real2(changed, deleted, sr)
        w2._run_pipeline = rec2
        assert _wait_for(lambda: calls2["n"] >= 1, timeout=40), \
            "dead 替换后新挂载周期未再补齐"
    finally:
        registry.stop_all()


# === 验收 3：默认项目 eager mount 不触发补齐 =====================================

def test_eager_default_mount_no_backfill(polling, fast_watch, tmp_path):
    """默认项目挂载不触发补齐（eager mount 无重建语义）——显式断言。"""
    import graphify.serve as S
    import rebuild_entry
    root = _mini_proj(tmp_path)
    rebuild_entry.rebuild(root)
    (root / "a.py").write_text(
        "import b\n\ndef foo():\n    return b.bar()\n\ndef eager_sym():\n    return 8\n",
        encoding="utf-8")
    assert "eager_sym()" not in _labels(root / "graphify-out"), "前置：图未含新语料"
    server = S._build_server(str(root / "graphify-out" / "graph.json"), watch=True)
    registry = server._graphify_registry
    try:
        wd = registry.get(root)
        assert wd is not None and wd.is_alive, "默认 watcher 未挂载"
        calls = {"n": 0}
        real = wd._run_pipeline

        def rec(changed, deleted, sr):
            calls["n"] += 1
            return real(changed, deleted, sr)
        wd._run_pipeline = rec
        time.sleep(1.2)  # 跨多个轮询周期（防抖 0.1 + 轮询 0.2；若 eager 补齐早已 flush）
        assert calls["n"] == 0, f"默认项目 eager mount 触发了补齐: {calls}"
        assert "eager_sym()" not in _labels(root / "graphify-out"), \
            "默认项目被无条件补齐重建（违反 US3 零回归）"
    finally:
        registry.stop_all()


# === 验收 4：跨进程互斥（真实 mkdir 锁双持有方竞争）==============================

def test_lock_busy_watcher_holds_lock_hook_exits_3(polling, tmp_path):
    """互斥方向 1：watcher 持锁重建期间，hook 面 rebuild_entry 拿锁失败 exit 3（锁忙）。"""
    import graphify.serve_watcher as W
    import rebuild_entry
    root = _mini_proj(tmp_path)
    rebuild_entry.rebuild(root)
    watcher = W.ServeWatcher(root, poll_interval=0.2, debounce=0.1)
    entered = threading.Event()
    release = threading.Event()
    real = watcher._run_pipeline

    def slow(changed, deleted, sr):
        entered.set()
        release.wait(30)  # 持锁阻塞重建（锁在 _flush_batch 获取，pipeline 全程持有）
        return real(changed, deleted, sr)
    watcher._run_pipeline = slow
    watcher.start()
    try:
        time.sleep(0.6)  # 基线
        (root / "a.py").write_text("x = 1\n", encoding="utf-8")
        assert entered.wait(15), "watcher 管线未进入（未持锁）"
        with pytest.raises(SystemExit) as ex:
            rebuild_entry.rebuild(root)
        assert ex.value.code == 3, "watcher 持锁期间 hook 应 exit 3（锁忙）"
    finally:
        release.set()
        watcher.stop()
        _cleanup(root)  # 锁目录卫生（防残留）


def test_lock_busy_hook_holds_lock_watcher_restores(polling, tmp_path):
    """互斥方向 2：hook 持锁期间 watcher 防抖到期 → 拿锁失败 restore 待重试、
    不丢批次、不持闸（释放信号量后重试）。"""
    import graphify.serve_watcher as W
    import rebuild_entry
    root = _mini_proj(tmp_path)
    rebuild_entry.rebuild(root)
    # hook 持锁（模拟 hook 重建中；无状态文件 → watcher 退化为当前指纹参照）
    assert rl._acquire_lock(root) is True, "测试前置：hook 未持到锁"
    gate = threading.Semaphore(1)
    watcher = W.ServeWatcher(root, out_dir=root / "graphify-out", gate=gate,
                             poll_interval=0.2, debounce=0.1)
    lock_busy_observed = threading.Event()
    real_flush = watcher._flush_batch

    def rec_flush(*a, **k):
        r = real_flush(*a, **k)
        if r is W._LOCK_BUSY:
            lock_busy_observed.set()
        return r
    watcher._flush_batch = rec_flush
    watcher.start()
    try:
        time.sleep(0.6)  # 基线
        (root / "a.py").write_text(
            "import b\n\ndef foo():\n    return b.bar()\n\ndef mutex_sym():\n    return 6\n",
            encoding="utf-8")
        assert lock_busy_observed.wait(15), "watcher 未观察到锁忙（未拿锁失败）"
        assert gate._value == 1, "锁忙期间持闸（信号量未释放，其他 watcher 会饿等）"
        assert "mutex_sym()" not in _labels(root / "graphify-out"), "持锁期间不应重建"
    finally:
        _cleanup(root)  # 释放 hook 锁
        assert _wait_for(lambda: "mutex_sym()" in _labels(root / "graphify-out"), timeout=40), \
            "锁释放后 watcher 重试未落地批次（事件丢失）"
        watcher.stop()
        _cleanup(root)


# === 验收 5：收敛语义——hook 收敛后 watcher 重试指纹命中跳过重建 ===================

def test_convergence_skips_redundant_build(polling, tmp_path):
    """收敛语义：hook 重建结果收敛后 watcher 重试时指纹命中 → 跳过实际重建（不重复 build）。"""
    import graphify.serve_watcher as W
    import rebuild_entry
    from fts_cache import fingerprint
    root = _mini_proj(tmp_path)
    rebuild_entry.rebuild(root)  # 基线 v1
    # 弄陈旧：改语料不重建 → graph 落后 v2
    (root / "a.py").write_text(
        "import b\n\ndef foo():\n    return b.bar()\n\ndef converge_sym():\n    return 11\n",
        encoding="utf-8")
    assert "converge_sym()" not in _labels(root / "graphify-out"), "前置：图未含新语料"
    # hook 持锁 + 写 rebuilding 状态（graph_fingerprint = 重建前 v1 指纹，schema v2）
    assert rl._acquire_lock(root) is True, "测试前置：hook 未持到锁"
    state = root / "graphify-out" / ".rebuild-state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({
        "schema": 2, "phase": "rebuilding", "started": time.time(),
        "project": str(root), "graph_fingerprint": list(fingerprint(root / "graphify-out" / "graph.json")),
    }), encoding="utf-8")
    watcher = W.ServeWatcher(root, out_dir=root / "graphify-out",
                             poll_interval=0.2, debounce=0.1)
    calls = {"n": 0}
    real = watcher._run_pipeline

    def rec(changed, deleted, sr):
        calls["n"] += 1
        return real(changed, deleted, sr)
    watcher._run_pipeline = rec
    lock_busy_observed = threading.Event()
    real_flush = watcher._flush_batch

    def rec_flush(*a, **k):
        r = real_flush(*a, **k)
        if r is W._LOCK_BUSY:
            lock_busy_observed.set()
        return r
    watcher._flush_batch = rec_flush
    watcher.start()
    try:
        time.sleep(0.6)  # 基线
        (root / "a.py").write_text(
            "import b\n\ndef foo():\n    return b.bar()\n\ndef converge_sym():\n    return 11\n"
            "\ndef edit_after():\n    return 13\n",
            encoding="utf-8")
        assert lock_busy_observed.wait(15), "watcher 未观察到锁忙"
        # hook 收敛：释放锁 + 真实 rebuild_entry 全量重建（graph 现含 converge_sym + edit_after）
        _cleanup(root)
        rebuild_entry.rebuild(root)
        assert "converge_sym()" in _labels(root / "graphify-out"), "hook 未收敛出新语料"
        assert "edit_after()" in _labels(root / "graphify-out"), "hook 收敛后编辑未入图"
        # watcher 重试：指纹命中（当前 ≠ hook 重建前参照）→ 跳过实际重建（不重复 build）
        time.sleep(2.5)  # 跨过锁忙退避重试窗口（backoff 1.0s），让重试完成跳过判定
        assert calls["n"] == 0, f"watcher 重复 build（收敛未跳过）: {calls}"
    finally:
        watcher.stop()
        _cleanup(root)


# === 验收 6：补齐走全局信号量排队，不阻塞查询响应，freshness 诚实标注 ================

def test_backfill_queues_through_gate_no_query_block(polling, fast_watch, tmp_path):
    """补齐走全局信号量排队，不阻塞查询响应：查询返回时补齐仍 rebuilding，
    状态文件 phase=rebuilding（freshness 信封诚实标注）。"""
    import graphify.serve as S
    import graphify.serve_watcher as W
    import rebuild_entry
    root = _mini_proj(tmp_path / "proj-a")
    proj_b = _mini_proj(tmp_path / "proj-b")
    rebuild_entry.rebuild(root)
    rebuild_entry.rebuild(proj_b)
    (proj_b / "a.py").write_text(
        "import b\n\ndef foo():\n    return b.bar()\n\ndef gate_sym():\n    return 1\n",
        encoding="utf-8")
    server = S._build_server(str(root / "graphify-out" / "graph.json"), watch=True)
    registry = server._graphify_registry
    try:
        server._graphify_select_graph(str(proj_b))  # 惰性挂载 + 补齐入队
        wb = registry.get(proj_b)
        assert wb is not None and wb.is_alive
        slow_started = threading.Event()
        release_slow = threading.Event()
        real = wb._run_pipeline

        def slow(changed, deleted, sr):
            slow_started.set()
            release_slow.wait(30)  # 补齐仍在 rebuilding（gate 已被占用）
            return real(changed, deleted, sr)
        wb._run_pipeline = slow
        # 等待补齐进入 pipeline（_begin_state 先写 rebuilding 状态，随后管线内联执行）
        assert slow_started.wait(15), "补齐未进入管线（gate 未占用）"
        # 查询响应不被补齐阻塞：再查 proj-b（alive 幂等，不碰 gate，立即返回）
        t0 = time.time()
        server._graphify_select_graph(str(proj_b))
        assert time.time() - t0 < 2.0, "查询被补齐重建阻塞"
        # freshness 信封诚实标注：补齐重建期间状态文件 phase=rebuilding
        state = proj_b / "graphify-out" / ".rebuild-state.json"
        data = json.loads(state.read_text(encoding="utf-8"))
        assert data["phase"] == "rebuilding", f"补齐期间状态文件应 rebuilding: {data}"
        from graphify.serve import _derive_freshness
        assert _derive_freshness(state) == "rebuilding", "freshness 信封未诚实标注 rebuilding"
    finally:
        release_slow.set()
        registry.stop_all()
        _cleanup(proj_b)


# === 验收 7：挂载补齐与挂载后即时编辑无缝衔接（并入同批次，不双跑）=================

def test_backfill_merges_immediate_edit(polling, tmp_path):
    """挂载补齐与挂载后即时编辑无缝衔接：补齐入队时挂载后发生的事件并入同批次（不双跑）。"""
    import graphify.serve_watcher as W
    import rebuild_entry
    root = _mini_proj(tmp_path)
    rebuild_entry.rebuild(root)
    registry = W.WatcherRegistry(_FakeCache(), debounce=0.5, poll_interval=0.2)
    w = registry.mount(root, root / "graphify-out")
    calls = {"n": 0}
    real = w._run_pipeline

    def rec(changed, deleted, sr):
        calls["n"] += 1
        return real(changed, deleted, sr)
    w._run_pipeline = rec
    try:
        # 挂载后立即编辑（在补齐的防抖窗内）——应并入同批次
        time.sleep(0.05)
        (root / "a.py").write_text(
            "import b\n\ndef foo():\n    return b.bar()\n\ndef merged_sym():\n    return 1\n",
            encoding="utf-8")
        assert _wait_for(lambda: "merged_sym()" in _labels(root / "graphify-out"), timeout=40), \
            "编辑未并入补齐批次"
        assert calls["n"] == 1, f"补齐与编辑双跑（未并批）: {calls}"
    finally:
        registry.stop_all()
        _cleanup(root)
