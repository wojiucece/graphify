"""per-project-watcher Task 02：WatcherRegistry + 双临时项目 E2E 基础。

spec 测试决策：
- 只测外部行为（文件/图状态），不断言 registry 内部数据结构（registry 独立单测除外）；
- 新接缝仅一个——WatcherRegistry 类独立单测（不依赖 serve 闭包环境）；
- Task 10 既有模式平移：防抖窗操控、真实 extract→build→FTS 管线、无 mock。

覆盖（任务验收清单）：
- 惰性挂载：查询项目 B → B 的 watcher 自动挂载并开始监听（registry 状态可查）
- 双临时项目 E2E 主断言 1：proj-a 编辑后 proj-a 图更新、proj-b 图字节不变
- 全局信号量 = 1：两项目同时编辑，重建管线互斥串行（等闸期间事件并入批次不丢）
- 幂等挂载两分支：alive 跳过（touch 使用序）/ dead 直接替换 / 并发首查只挂一个
- 默认项目 eager mount 默认布局外部行为与 Task 10 一致（_build_server 既有测试零回归）
- out_dir 对齐：GRAPHIFY_OUT 覆盖布局下 watcher 监听目标与查询目标是同一 graph.json
"""
import json
import threading
import time
from pathlib import Path

import pytest


def _mini_proj(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "a.py").write_text(
        "import b\n\ndef foo():\n    return b.bar()\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("def bar():\n    return 1\n", encoding="utf-8")
    return tmp_path


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


@pytest.fixture
def out_override(monkeypatch):
    """GRAPHIFY_OUT 相对覆盖（in-process 三处引用同步：paths + serve_watcher 快照）。

    graphify.paths.GRAPHIFY_OUT 是 import 时冻结的模块常量，serve 解析链与 watcher
    排除逻辑各自持引用，覆盖时必须三处一起 patch（test_hook_guard.py 同款手法）。
    """
    import graphify.paths as P
    import graphify.serve_watcher as W
    monkeypatch.setattr(P, "GRAPHIFY_OUT", "graphify-custom-out")
    monkeypatch.setattr(P, "GRAPHIFY_OUT_NAME", "graphify-custom-out")
    monkeypatch.setattr(W, "_GRAPHIFY_OUT", "graphify-custom-out")
    monkeypatch.setattr(W, "_GRAPHIFY_OUT_NAME", "graphify-custom-out")


# === WatcherRegistry 独立单测（新接缝，不依赖 serve 闭包环境）==================

def test_registry_mount_creates_watcher(polling, tmp_path):
    """fresh mount：返回新 watcher，registry 状态可查（get/status_summary）。"""
    import graphify.serve_watcher as W
    root = _mini_proj(tmp_path)
    registry = W.WatcherRegistry(_FakeCache(), poll_interval=0.2)
    w = registry.mount(root, root / "graphify-out")
    try:
        assert w is not None
        assert w.is_alive
        assert registry.get(root) is w
        assert registry.get(tmp_path / "other") is None
        summary = registry.status_summary()
        assert len(summary) == 1
        assert summary[0]["project_root"] == str(Path(root).resolve())
        assert summary[0]["status"] == "active"
        assert summary[0]["backend"] in ("watchdog", "polling")
    finally:
        registry.stop_all()


def test_registry_mount_idempotent_alive_skip(polling, tmp_path):
    """幂等分支 1：alive watcher 存在 → 跳过复用（同一实例，仅 touch 使用序）。"""
    import graphify.serve_watcher as W
    root = _mini_proj(tmp_path)
    registry = W.WatcherRegistry(_FakeCache(), poll_interval=0.2)
    w1 = registry.mount(root, root / "graphify-out")
    try:
        w2 = registry.mount(root, root / "graphify-out")
        assert w2 is w1, "alive watcher 存在时应跳过复用"
        assert len(registry.status_summary()) == 1
    finally:
        registry.stop_all()


def test_registry_mount_replaces_dead_watcher(polling, tmp_path, monkeypatch):
    """幂等分支 2：查询时发现 dead watcher → 直接替换为全新实例（不经逐出）。"""
    import graphify.serve_watcher as W
    import rebuild_entry
    root = _mini_proj(tmp_path)
    rebuild_entry.rebuild(root)
    monkeypatch.setattr(W, "_MAX_SYNC_FAILURE_RETRIES", 2)
    registry = W.WatcherRegistry(_FakeCache(), debounce=0.1, poll_interval=0.2)
    w1 = registry.mount(root, root / "graphify-out")

    def boom(changed, deleted, sr):
        raise RuntimeError("boom")
    w1._run_pipeline = boom
    try:
        time.sleep(0.6)  # 等基线快照（首次扫描只建基线）
        (root / "a.py").write_text("x = 1\n", encoding="utf-8")
        assert _wait_for(lambda: not w1.is_alive), "连续失败后 watcher 未自禁用"
        w2 = registry.mount(root, root / "graphify-out")
        assert w2 is not w1, "dead watcher 应被替换为全新实例"
        assert w2.is_alive
        assert registry.get(root) is w2
        assert len(registry.status_summary()) == 1
    finally:
        registry.stop_all()


def test_registry_concurrent_mount_mounts_once(polling, tmp_path):
    """并发首查同一项目：双检查只挂一个 watcher。"""
    import graphify.serve_watcher as W
    root = _mini_proj(tmp_path)
    registry = W.WatcherRegistry(_FakeCache(), poll_interval=0.2)
    barrier = threading.Barrier(2)
    results = []

    def worker():
        barrier.wait()
        results.append(registry.mount(root, root / "graphify-out"))

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    t2.start()
    t1.join(30)
    t2.join(30)
    try:
        assert not t1.is_alive() and not t2.is_alive(), "并发 worker 未退出"
        assert len(results) == 2
        assert results[0] is results[1], "并发挂载同一项目产生两个 watcher"
        assert registry.get(root) is results[0]
        assert len(registry.status_summary()) == 1
    finally:
        registry.stop_all()


# === 双临时项目 E2E：主断言 1（项目隔离）+ 全局信号量 = 1 =======================

def test_registry_dual_project_isolation(polling, tmp_path):
    """主断言 1：proj-a 编辑（防抖窗操控）后 proj-a 图更新、proj-b 图字节不变。"""
    import graphify.serve_watcher as W
    import rebuild_entry
    proj_a = _mini_proj(tmp_path / "proj-a")
    proj_b = _mini_proj(tmp_path / "proj-b")
    rebuild_entry.rebuild(proj_a)
    rebuild_entry.rebuild(proj_b)
    out_a, out_b = proj_a / "graphify-out", proj_b / "graphify-out"
    b_before = (out_b / "graph.json").read_bytes()
    registry = W.WatcherRegistry(_FakeCache(), debounce=0.1, poll_interval=0.2)
    registry.mount(proj_a, out_a)
    registry.mount(proj_b, out_b)
    try:
        time.sleep(0.6)  # 等两 watcher 基线快照
        (proj_a / "a.py").write_text(
            "import b\n\ndef foo():\n    return b.bar()\n\ndef iso_a():\n    return 2\n",
            encoding="utf-8")
        assert _wait_for(lambda: "iso_a()" in _labels(out_a)), "proj-a 图未更新"
        assert (out_b / "graph.json").read_bytes() == b_before, "proj-b 图字节被改动"
        assert "iso_a()" not in _labels(out_b), "proj-b 图被 proj-a 编辑污染"
    finally:
        registry.stop_all()


def test_registry_global_semaphore_serializes_rebuilds(polling, tmp_path):
    """全局信号量 = 1：两项目同时编辑，两个 rebuild 不并发执行（max == 1）；
    等闸期间新事件并入各自批次不丢（beta 与 beta2 都进图）。"""
    import graphify.serve_watcher as W
    import rebuild_entry
    proj_a = _mini_proj(tmp_path / "proj-a")
    proj_b = _mini_proj(tmp_path / "proj-b")
    rebuild_entry.rebuild(proj_a)
    rebuild_entry.rebuild(proj_b)
    out_a, out_b = proj_a / "graphify-out", proj_b / "graphify-out"
    registry = W.WatcherRegistry(_FakeCache(), debounce=0.1, poll_interval=0.2)
    wa = registry.mount(proj_a, out_a)
    wb = registry.mount(proj_b, out_b)
    state = {"cur": 0, "max": 0, "lock": threading.Lock()}
    real_a, real_b = wa._run_pipeline, wb._run_pipeline
    entered = threading.Event()

    def recorder(real, sleep: float):
        def rec(changed, deleted, sr):
            if sleep:
                entered.set()
                time.sleep(sleep)  # 持闸（闸在 _flush_batch 获取）让对侧批次等闸
            with state["lock"]:
                state["cur"] += 1
                state["max"] = max(state["max"], state["cur"])
            try:
                return real(changed, deleted, sr)
            finally:
                with state["lock"]:
                    state["cur"] -= 1
        return rec

    wa._run_pipeline = recorder(real_a, sleep=1.0)
    wb._run_pipeline = recorder(real_b, sleep=0.0)
    try:
        time.sleep(0.6)  # 基线
        (proj_a / "a.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
        assert entered.wait(15), "proj-a 管线未进入（未持闸）"
        # A 持闸期间并发编辑 B 两次：事件必须并入 B 各自批次，不丢
        (proj_b / "a.py").write_text(
            "def beta():\n    return 1\n", encoding="utf-8")
        (proj_b / "a.py").write_text(
            "def beta():\n    return 1\n\ndef beta2():\n    return 2\n", encoding="utf-8")
        assert _wait_for(lambda: "alpha()" in _labels(out_a)), "proj-a 图未更新"
        assert _wait_for(lambda: "beta()" in _labels(out_b)), "proj-b 首批事件丢失"
        assert _wait_for(lambda: "beta2()" in _labels(out_b)), "proj-b 等闸期间事件丢失"
        assert state["max"] == 1, f"两个 watcher 重建并发执行: max={state['max']}"
    finally:
        registry.stop_all()


# === serve 层：默认 eager mount / 惰性挂载接线 / out_dir 对齐 ===================

def test_serve_eager_default_mount(polling, fast_watch, tmp_path):
    """默认项目 eager mount：registry 内默认 watcher 挂上（registry 状态可查）。"""
    import graphify.serve as S
    import rebuild_entry
    root = _mini_proj(tmp_path)
    rebuild_entry.rebuild(root)
    server = S._build_server(str(root / "graphify-out" / "graph.json"), watch=True)
    registry = getattr(server, "_graphify_registry", None)
    try:
        assert registry is not None
        summary = registry.status_summary()
        assert len(summary) == 1, f"默认 eager mount 应恰一个 watcher: {summary}"
        assert summary[0]["project_root"] == str(Path(root).resolve())
        assert getattr(server, "_graphify_watcher", None) is registry.get(root)
    finally:
        registry.stop_all()


def test_serve_lazy_mount_on_query(polling, fast_watch, tmp_path):
    """惰性挂载：查询项目 B → B 的 watcher 自动挂载并监听（编辑 B → B 图更新，
    A 图不变）；重复查询幂等（同一实例）。"""
    import graphify.serve as S
    import rebuild_entry
    root = _mini_proj(tmp_path / "proj-a")
    proj_b = _mini_proj(tmp_path / "proj-b")
    rebuild_entry.rebuild(root)
    rebuild_entry.rebuild(proj_b)
    server = S._build_server(str(root / "graphify-out" / "graph.json"), watch=True)
    registry = server._graphify_registry
    try:
        assert registry.get(proj_b) is None, "前置：proj-b 未挂载"
        server._graphify_select_graph(str(proj_b))
        wb = registry.get(proj_b)
        assert wb is not None, "查询 proj-b 后 watcher 未自动挂载"
        assert wb.is_alive
        wb2 = registry.get(proj_b)
        server._graphify_select_graph(str(proj_b))  # 重复查询
        assert registry.get(proj_b) is wb2, "重复查询应幂等（不重复挂载）"
        time.sleep(0.6)  # proj-b watcher 基线
        (proj_b / "a.py").write_text(
            "import b\n\ndef foo():\n    return b.bar()\n\ndef lazy_sym():\n    return 3\n",
            encoding="utf-8")
        assert _wait_for(lambda: "lazy_sym()" in _labels(proj_b / "graphify-out")), \
            "惰性挂载的 watcher 未驱动 proj-b 重建"
        assert "lazy_sym()" not in _labels(root / "graphify-out"), "proj-a 图被污染"
    finally:
        registry.stop_all()


def test_serve_out_dir_alignment_override_layout(polling, fast_watch, out_override, tmp_path):
    """out_dir 对齐：GRAPHIFY_OUT 相对覆盖布局下，watcher 监听的 rebuild 目标与
    查询目标是同一个 graph.json（不再 parent.parent 反推；默认 eager mount 同走解析链）。"""
    import graphify.serve as S
    import rebuild_entry
    root = _mini_proj(tmp_path / "proj-a")
    proj_b = _mini_proj(tmp_path / "proj-b")
    out_a = root / "graphify-custom-out"
    out_b = proj_b / "graphify-custom-out"
    rebuild_entry.rebuild(root, out_dir=out_a)
    rebuild_entry.rebuild(proj_b, out_dir=out_b)
    assert (out_b / "graph.json").exists(), "前置：覆盖布局下 proj-b 图在 override 目录"
    assert not (proj_b / "graphify-out" / "graph.json").exists(), \
        "前置：覆盖布局下 proj-b 图不在默认 graphify-out"
    server = S._build_server(str(out_a / "graph.json"), watch=True)
    registry = server._graphify_registry
    try:
        # 默认 eager mount：监听根 = proj-a（标准布局推导，非 parent.parent 无条件反推）
        assert registry.get(root) is not None
        # 惰性挂载 proj-b：watcher 的 out_dir 来自查询侧解析链 → override 目录
        server._graphify_select_graph(str(proj_b))
        assert registry.get(proj_b) is not None
        time.sleep(0.6)  # proj-b watcher 基线
        (proj_b / "a.py").write_text(
            "import b\n\ndef foo():\n    return b.bar()\n\ndef align_sym():\n    return 4\n",
            encoding="utf-8")
        assert _wait_for(lambda: "align_sym()" in _labels(out_b)), \
            "watcher 重建目标 ≠ 查询目标（override graph.json 未更新）"
        assert "align_sym()" not in _labels(out_a), "proj-a 图被 proj-b 编辑污染"
        assert "align_sym()" not in _labels(proj_b / "graphify-out"), \
            "默认 graphify-out 不应出现 override 布局的产物"
    finally:
        registry.stop_all()
