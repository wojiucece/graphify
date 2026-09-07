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


# === 票 03：上限逐出 / 使用序 / dead 优先 / pinned 豁免 / on_evict 停 watcher ========

def test_registry_max_watchers_defaults_and_fallback(monkeypatch):
    """GRAPHIFY_MAX_WATCHERS 默认跟随生效 ctx 上限（含 GRAPHIFY_MAX_CONTEXTS 覆盖）；
    无效值回退默认（对齐 serve._max_server_contexts 模式）。"""
    import graphify.serve_watcher as W
    monkeypatch.delenv("GRAPHIFY_MAX_WATCHERS", raising=False)
    monkeypatch.delenv("GRAPHIFY_MAX_CONTEXTS", raising=False)
    assert W.WatcherRegistry(_FakeCache())._max_watchers == 8
    monkeypatch.setenv("GRAPHIFY_MAX_CONTEXTS", "3")
    assert W.WatcherRegistry(_FakeCache())._max_watchers == 3, "默认未跟随 GRAPHIFY_MAX_CONTEXTS"
    monkeypatch.setenv("GRAPHIFY_MAX_WATCHERS", "5")
    assert W.WatcherRegistry(_FakeCache())._max_watchers == 5, "显式值未优先"
    monkeypatch.setenv("GRAPHIFY_MAX_WATCHERS", "abc")
    assert W.WatcherRegistry(_FakeCache())._max_watchers == 3, "无效值未回退默认（ctx 上限）"


def test_registry_evict_graph_stops_and_removes(polling, tmp_path):
    """LRU 联动逐出：registry.evict_graph(graph_path) 停掉对应 watcher 并从 registry 移除；
    未挂载该图则 no-op（幂等）。"""
    import graphify.serve_watcher as W
    proj_a = _mini_proj(tmp_path / "proj-a")
    proj_b = _mini_proj(tmp_path / "proj-b")
    registry = W.WatcherRegistry(_FakeCache(), poll_interval=0.2)
    wa = registry.mount(proj_a, proj_a / "graphify-out")
    wb = registry.mount(proj_b, proj_b / "graphify-out")
    graph_a = str((proj_a / "graphify-out" / "graph.json").resolve())
    try:
        registry.evict_graph(graph_a)
        assert not wa.is_alive, "evict_graph 未停止 watcher"
        assert registry.get(proj_a) is None, "evict_graph 未从 registry 移除"
        assert wb.is_alive, "非目标 watcher 被误停"
        registry.evict_graph(graph_a)  # 已移除 → no-op，不炸
        registry.evict_graph(str((tmp_path / "nope" / "graph.json").resolve()))
    finally:
        registry.stop_all()


def test_registry_cap_evicts_lru_by_usage(polling, tmp_path, monkeypatch):
    """上限逐旧按使用序（select 命中 touch），非挂载序——活跃项目不被误伤。"""
    import graphify.serve_watcher as W
    monkeypatch.setenv("GRAPHIFY_MAX_WATCHERS", "2")
    proj_a = _mini_proj(tmp_path / "proj-a")
    proj_b = _mini_proj(tmp_path / "proj-b")
    proj_c = _mini_proj(tmp_path / "proj-c")
    registry = W.WatcherRegistry(_FakeCache(), poll_interval=0.2)
    wa = registry.mount(proj_a, proj_a / "graphify-out")
    wb = registry.mount(proj_b, proj_b / "graphify-out")
    registry.mount(proj_a, proj_a / "graphify-out")  # 命中 proj-a → touch 使用序 [b, a]
    wc = registry.mount(proj_c, proj_c / "graphify-out")  # 超上限 → 逐最近最少使用 = b
    try:
        assert not wb.is_alive, "LRU（b）未被逐出（按挂载序逐了 a 才会错）"
        assert registry.get(proj_b) is None
        assert wa.is_alive and wc.is_alive
    finally:
        registry.stop_all()


def test_registry_cap_evicts_dead_before_alive(polling, tmp_path, monkeypatch):
    """dead watcher 优先于 alive 被清（腾位先扫 dead）。"""
    import graphify.serve_watcher as W
    import rebuild_entry
    monkeypatch.setenv("GRAPHIFY_MAX_WATCHERS", "2")
    monkeypatch.setattr(W, "_MAX_SYNC_FAILURE_RETRIES", 2)
    proj_a = _mini_proj(tmp_path / "proj-a")
    proj_b = _mini_proj(tmp_path / "proj-b")
    proj_c = _mini_proj(tmp_path / "proj-c")
    rebuild_entry.rebuild(proj_a)
    registry = W.WatcherRegistry(_FakeCache(), debounce=0.1, poll_interval=0.2)
    wa = registry.mount(proj_a, proj_a / "graphify-out")
    wb = registry.mount(proj_b, proj_b / "graphify-out")

    def boom(changed, deleted, sr):
        raise RuntimeError("boom")
    wa._run_pipeline = boom
    try:
        time.sleep(0.6)  # 等基线快照
        (proj_a / "a.py").write_text("x = 1\n", encoding="utf-8")
        assert _wait_for(lambda: not wa.is_alive, timeout=40), "a 未自禁用"
        wc = registry.mount(proj_c, proj_c / "graphify-out")  # 超上限 → 先清 dead = a
        assert registry.get(proj_a) is None, "dead watcher 未优先被清"
        assert wb.is_alive, "alive watcher 被误清"
        assert wc.is_alive
    finally:
        registry.stop_all()


def test_registry_cap_pinned_exempt(polling, tmp_path, monkeypatch):
    """pinned 默认 watcher 不占 GRAPHIFY_MAX_WATCHERS 配额、永不被上限逐出。"""
    import graphify.serve_watcher as W
    monkeypatch.setenv("GRAPHIFY_MAX_WATCHERS", "1")
    proj_default = _mini_proj(tmp_path / "proj-default")
    proj_a = _mini_proj(tmp_path / "proj-a")
    proj_b = _mini_proj(tmp_path / "proj-b")
    registry = W.WatcherRegistry(_FakeCache(), poll_interval=0.2)
    wd = registry.mount(proj_default, proj_default / "graphify-out", pinned=True)
    wa = registry.mount(proj_a, proj_a / "graphify-out")   # 非 pinned 数 = 1 = 配额
    wb = registry.mount(proj_b, proj_b / "graphify-out")   # 非 pinned 数 = 2 > 1 → 逐 LRU = a
    try:
        assert wd.is_alive, "pinned 默认 watcher 被上限逐出"
        assert registry.get(proj_default) is wd
        assert not wa.is_alive, "LRU（a）未被逐出"
        assert wb.is_alive
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


# === 票 03 E2E：LRU 联动逐出 / 自禁用复活 / on_evict 不在 invalidate 触发 ===========

def test_cache_on_evict_not_triggered_by_invalidate(tmp_path):
    """on_evict 仅 LRU 容量逐出触发，invalidate() 的 pop 不触发（否则 pipeline 完成即停 watcher）。"""
    import graphify.serve as S
    import rebuild_entry
    proj_a = _mini_proj(tmp_path / "proj-a")
    proj_b = _mini_proj(tmp_path / "proj-b")
    proj_c = _mini_proj(tmp_path / "proj-c")
    rebuild_entry.rebuild(proj_a)
    rebuild_entry.rebuild(proj_b)
    rebuild_entry.rebuild(proj_c)
    evicted = []
    cache = S._GraphContextCache(1, on_evict=evicted.append)
    pa = str((proj_a / "graphify-out" / "graph.json").resolve())
    pb = str((proj_b / "graphify-out" / "graph.json").resolve())
    pc = str((proj_c / "graphify-out" / "graph.json").resolve())
    cache.load(pa)
    assert evicted == []
    cache.invalidate(pa)  # pipeline 完成的路径：不应触发 on_evict
    assert evicted == [], "invalidate() 触发了 on_evict"
    cache.load(pb)
    assert evicted == []
    cache.load(pc)  # 超容量 → 逐出 pb
    assert pb in evicted, "LRU 容量逐出未触发 on_evict"
    assert pa not in evicted and pc not in evicted


def test_pipeline_complete_invalidate_keeps_watcher_alive(polling, fast_watch, tmp_path):
    """on_evict 不在 invalidate() 触发（E2E）：pipeline 完成（触发 invalidate）后 watcher 仍 alive。

    若 on_evict 误挂在 invalidate() 上，每次重建完成都会停掉刚干完活的 watcher——默认项目
    watcher 会在第一次重建后死亡，违反单项目零回归承诺。
    """
    import graphify.serve as S
    import rebuild_entry
    root = _mini_proj(tmp_path)
    rebuild_entry.rebuild(root)
    server = S._build_server(str(root / "graphify-out" / "graph.json"), watch=True)
    registry = server._graphify_registry
    try:
        wa = registry.get(root)
        assert wa is not None and wa.is_alive
        time.sleep(0.6)  # 基线
        (root / "a.py").write_text(
            "import b\n\ndef foo():\n    return b.bar()\n\ndef keep_alive():\n    return 7\n",
            encoding="utf-8")
        assert _wait_for(lambda: "keep_alive()" in _labels(root / "graphify-out")), \
            "pipeline 未完成（图未更新）"
        assert wa.is_alive, "pipeline 完成（invalidate）误停了 watcher"
    finally:
        registry.stop_all()


def test_serve_lru_eviction_stops_watcher(polling, fast_watch, tmp_path, monkeypatch):
    """主断言 2：GRAPHIFY_MAX_CONTEXTS=1 时查询 proj-b → proj-a ctx 被逐出 → proj-a
    watcher 停止（registry 状态断言：移除 + 线程死亡）。"""
    import graphify.serve as S
    import rebuild_entry
    monkeypatch.setenv("GRAPHIFY_MAX_CONTEXTS", "1")
    proj_default = _mini_proj(tmp_path / "proj-default")
    proj_a = _mini_proj(tmp_path / "proj-a")
    proj_b = _mini_proj(tmp_path / "proj-b")
    rebuild_entry.rebuild(proj_default)
    rebuild_entry.rebuild(proj_a)
    rebuild_entry.rebuild(proj_b)
    server = S._build_server(str(proj_default / "graphify-out" / "graph.json"), watch=True)
    registry = server._graphify_registry
    try:
        server._graphify_select_graph(str(proj_a))
        wa = registry.get(proj_a)
        assert wa is not None and wa.is_alive
        server._graphify_select_graph(str(proj_b))
        assert not wa.is_alive, "proj-a watcher 未随 ctx 逐出停止"
        assert registry.get(proj_a) is None, "逐出后 proj-a 应从 registry 移除"
        wb = registry.get(proj_b)
        assert wb is not None and wb.is_alive, "proj-b watcher 未挂载"
        wd = registry.get(proj_default)
        assert wd is not None and wd.is_alive, "默认 pinned watcher 被误停"
    finally:
        registry.stop_all()


def test_serve_self_disabled_revives_fresh_watcher(polling, fast_watch, tmp_path, monkeypatch):
    """主断言 3：注入连续失败使 proj-a watcher 自禁用 → 逐出 → 重查 proj-a → 全新 watcher
    挂载且失败计数从零（重入复活，新实例未继承禁用状态）。"""
    import graphify.serve as S
    import graphify.serve_watcher as W
    import rebuild_entry
    monkeypatch.setenv("GRAPHIFY_MAX_CONTEXTS", "1")
    monkeypatch.setattr(W, "_MAX_SYNC_FAILURE_RETRIES", 2)
    proj_default = _mini_proj(tmp_path / "proj-default")
    proj_a = _mini_proj(tmp_path / "proj-a")
    proj_b = _mini_proj(tmp_path / "proj-b")
    rebuild_entry.rebuild(proj_default)
    rebuild_entry.rebuild(proj_a)
    rebuild_entry.rebuild(proj_b)
    server = S._build_server(str(proj_default / "graphify-out" / "graph.json"), watch=True)
    registry = server._graphify_registry
    try:
        server._graphify_select_graph(str(proj_a))
        wa = registry.get(proj_a)
        assert wa is not None and wa.is_alive

        def boom(changed, deleted, sr):
            raise RuntimeError("boom")
        wa._run_pipeline = boom
        time.sleep(0.6)  # 基线
        (proj_a / "a.py").write_text("x = 1\n", encoding="utf-8")
        assert _wait_for(lambda: not wa.is_alive, timeout=40), "proj-a watcher 未自禁用"

        server._graphify_select_graph(str(proj_b))  # 逐出 proj-a ctx → 移除其 watcher
        assert registry.get(proj_a) is None, "逐出后 proj-a 应从 registry 移除"

        server._graphify_select_graph(str(proj_a))  # 重查 → 全新 watcher
        wa2 = registry.get(proj_a)
        assert wa2 is not None and wa2 is not wa, "重查未挂载全新 watcher"
        assert wa2.is_alive

        time.sleep(0.6)  # 新 watcher 基线
        (proj_a / "a.py").write_text("def revived():\n    return 1\n", encoding="utf-8")
        assert _wait_for(lambda: "revived()" in _labels(proj_a / "graphify-out")), \
            "新 watcher 未成功重建（疑似继承了禁用状态）"
        assert wa2.is_alive, "新 watcher 重建后死亡"
    finally:
        registry.stop_all()


def test_lock_order_evict_vs_pipeline_complete_no_deadlock(polling, tmp_path):
    """锁序无死锁：watcher pipeline 完成回调（invalidate）抢缓存锁的同时触发逐出。

    坏实现（on_evict 在缓存锁内调 stop→join）死锁：逐出线程持缓存锁 join watcher 线程，
    watcher 线程的 invalidate 抢缓存锁 → 互相等待。本实现回调在缓存锁外执行——load 释放
    缓存锁后才 on_evict，invalidate 立即拿到锁。用真实管线 + 有界等待构造竞态（非时序碰巧）。
    """
    import graphify.serve as S
    import graphify.serve_watcher as W
    import rebuild_entry
    proj_a = _mini_proj(tmp_path / "proj-a")
    proj_b = _mini_proj(tmp_path / "proj-b")
    rebuild_entry.rebuild(proj_a)
    rebuild_entry.rebuild(proj_b)
    out_a, out_b = proj_a / "graphify-out", proj_b / "graphify-out"
    cache = S._GraphContextCache(1)
    registry = W.WatcherRegistry(cache, poll_interval=0.2)
    # registry 构造时已把 cache._on_evict 接到 evict_graph（真实接线，非测试手工挂）
    wa = registry.mount(proj_a, out_a)
    cache.load(str((out_a / "graph.json").resolve()))  # proj-a ctx 占满 1 槽
    entered = threading.Event()
    go = threading.Event()
    real = wa._run_pipeline

    def slow(changed, deleted, sr):
        entered.set()
        go.wait(30)  # 有界等待主线程开始逐出（防自身死锁）
        return real(changed, deleted, sr)

    wa._run_pipeline = slow
    elapsed = {}
    try:
        time.sleep(0.6)  # 基线
        (proj_a / "a.py").write_text("def lock_sym():\n    return 1\n", encoding="utf-8")
        assert entered.wait(15), "watcher 管线未进入（竞态未构造）"
        go.set()

        def do_evict():
            t0 = time.time()
            cache.load(str((out_b / "graph.json").resolve()))
            elapsed["t"] = time.time() - t0

        evict_thread = threading.Thread(target=do_evict, daemon=True)
        evict_thread.start()
        evict_thread.join(40)
        assert not evict_thread.is_alive(), "逐出线程死锁（on_evict 疑似在缓存锁内执行）"
        assert elapsed["t"] < 30, f"逐出-join 异常耗时: {elapsed['t']:.1f}s"
        assert not wa.is_alive, "proj-a watcher 线程未退出（逐出 stop 未完成）"
    finally:
        registry.stop_all()
