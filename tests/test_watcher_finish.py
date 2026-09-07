"""per-project-watcher Task 05（收尾票）：可观测性 + 嵌套边界 + 停机协议。

spec 测试决策（与票 02/04 同构）：
- 只测外部行为（图/状态文件/响应内容），不断言 registry 内部队列结构；
- E2E 走真实管线（真实 extract→build→FTS，无 mock）；
- 停机协议用真实信号量 + 真实双项目 pending 批次；
- observer 泄漏修复：真实 watchdog 后端回归（watchdog 未装则跳过）+ 确定性 fake observer
  直测（无 watchdog 环境也覆盖——polling fixture 掩盖正是该 bug 藏身之处）。

覆盖（任务验收清单 + 控制器交接）：
- 嵌套 E2E：非隐藏子项目在父项目内各有 graphify-out → 同一次编辑父子各自更新各图，互不污染
- 停机 flush 不丢：双项目各有 pending 批次 stop → 两图均反映（先全发信号再依序 join）
- 停机 join 序：信号量持有者优先（mid-build 先 join，其 final flush 释放闸）
- graph_stats watched_projects：多 watcher 列全 / 零 watcher 为 [] / 正文不变（加性）
- WatcherRegistry 独立单测：status 汇总（自禁用 → status=disabled，词表统一）
- LRU 逐出重入 = 新挂载周期 → 补齐一次（票 03 逐出 × 票 04 补齐组合语义）
- 默认 pinned 自禁用防回归：默认图查询不复活（死亡直到重启）；显式 project_path 走 dead-replace
- observer 泄漏修复：自禁用后 stop() 必须停/join observer（真实 watchdog + 确定性两路）
- FB3 耗尽路径：3 次 final-flush 重试全失败 → 诚实告警 + pending 计数
- 挂载补齐 merged batch freshness 标注（周期标志替代空批次检测）
- stop_all 与并发挂载竞态不变式：新挂载 watcher 由 atexit 兜底
- stderr 日志全景：挂载/补齐入队/上限逐出/LRU 逐出/自禁用/复活均有单行日志
"""
import json
import shutil
import threading
import time
from pathlib import Path

import pytest

import graphify.rebuild_lock as rl


def _mini_proj(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "a.py").write_text(
        "import b\n\ndef foo():\n    return b.bar()\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("def bar():\n    return 1\n", encoding="utf-8")
    return tmp_path


def _wait_for(pred, timeout: float = 40.0, interval: float = 0.1) -> bool:
    """轮询直至 pred() 为真。容忍 watcher 线程并发原子替换 graph.json 的瞬时
    Windows 文件锁（PermissionError ⊂ OSError）——既有 _wait_for 模式平移。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if pred():
                return True
        except OSError:
            pass
        time.sleep(interval)
    try:
        return bool(pred())
    except OSError:
        return False


def _labels(out_dir: Path) -> set:
    g = out_dir / "graph.json"
    if not g.exists():
        return set()
    data = json.loads(g.read_text(encoding="utf-8"))
    return {n["label"] for n in data.get("nodes", [])}


def _cleanup(root: Path) -> None:
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


# === 验收 1：嵌套 E2E——非隐藏子项目各建各图，互不污染 ==============================

def test_nested_subproject_e2e(polling, tmp_path):
    """非隐藏子项目位于父项目内且各有 graphify-out → 同一次编辑父 watcher 与子
    watcher 各自更新各图，互不干扰（各建各图；父图含子项目文件、子图不含父项目独有文件）。"""
    import graphify.serve_watcher as W
    import rebuild_entry
    parent = _mini_proj(tmp_path / "parent")
    child = parent / "child"
    child.mkdir()
    (child / "c.py").write_text("def child_orig():\n    return 1\n", encoding="utf-8")
    rebuild_entry.rebuild(parent)
    rebuild_entry.rebuild(child)
    out_parent = parent / "graphify-out"
    out_child = child / "graphify-out"
    assert "child_orig()" in _labels(out_parent), "前置：父图应含子项目文件（非隐藏嵌套）"
    registry = W.WatcherRegistry(_FakeCache(), debounce=0.1, poll_interval=0.2)
    wp = registry.mount(parent, out_parent)
    wc = registry.mount(child, out_child)
    try:
        time.sleep(0.6)  # 基线
        (child / "c.py").write_text(
            "def child_orig():\n    return 1\n\ndef child_new():\n    return 2\n",
            encoding="utf-8")
        assert _wait_for(lambda: "child_new()" in _labels(out_parent)), "父图未更新子项目编辑"
        assert _wait_for(lambda: "child_new()" in _labels(out_child)), "子图未更新自身编辑"
        # 互不污染：父项目独有编辑不进子图
        (parent / "a.py").write_text(
            "import b\n\ndef foo():\n    return b.bar()\n\ndef parent_only():\n    return 3\n",
            encoding="utf-8")
        assert _wait_for(lambda: "parent_only()" in _labels(out_parent)), "父图未更新 parent_only"
        assert "parent_only()" not in _labels(out_child), "子图被父项目独有编辑污染"
    finally:
        registry.stop_all()
        _cleanup(parent)
        _cleanup(child)


# === 验收 2：停机 flush 不丢 + 信号量持有者优先 join ================================

def test_stop_all_flushes_both_pending_batches(polling, tmp_path):
    """双项目各有 pending 批次时 stop → 两图均反映 pending 变更（先全发停止信号再
    依序 join；各 watcher 自身线程 final flush 落盘，不丢事件）。"""
    import graphify.serve_watcher as W
    import rebuild_entry
    proj_a = _mini_proj(tmp_path / "proj-a")
    proj_b = _mini_proj(tmp_path / "proj-b")
    rebuild_entry.rebuild(proj_a)
    rebuild_entry.rebuild(proj_b)
    registry = W.WatcherRegistry(_FakeCache(), debounce=10.0, poll_interval=0.2)
    wa = registry.mount(proj_a, proj_a / "graphify-out")
    wb = registry.mount(proj_b, proj_b / "graphify-out")
    try:
        time.sleep(0.6)  # 基线
        (proj_a / "a.py").write_text(
            "import b\n\ndef foo():\n    return b.bar()\n\ndef shut_a():\n    return 1\n",
            encoding="utf-8")
        (proj_b / "b.py").write_text(
            "def bar():\n    return 1\n\ndef shut_b():\n    return 2\n",
            encoding="utf-8")
        time.sleep(0.5)  # 编辑进 pending（防抖 10s 未到，主循环不 flush）
        registry.stop_all()
        assert "shut_a()" in _labels(proj_a / "graphify-out"), "proj-a pending 批次未 flush"
        assert "shut_b()" in _labels(proj_b / "graphify-out"), "proj-b pending 批次未 flush"
    finally:
        registry.stop_all()


def test_stop_all_semaphore_holder_joins_first(polling, tmp_path):
    """信号量持有者优先 join：mid-build 的 watcher 先 join（其 final flush 完成释放闸，
    后续 join 不堵闸不丢 pending）。用 _join_and_finish 打点记录 join 序。"""
    import graphify.serve_watcher as W
    import rebuild_entry
    proj_a = _mini_proj(tmp_path / "proj-a")
    proj_b = _mini_proj(tmp_path / "proj-b")
    rebuild_entry.rebuild(proj_a)
    rebuild_entry.rebuild(proj_b)
    registry = W.WatcherRegistry(_FakeCache(), debounce=0.1, poll_interval=0.2)
    # 先挂 b（插入序 [b, a]）——若停机按插入序 join，b 会先；信号量持有者优先则 a 先。
    wb = registry.mount(proj_b, proj_b / "graphify-out")
    wa = registry.mount(proj_a, proj_a / "graphify-out")
    order = []
    real_join_a, real_join_b = wa._join_and_finish, wb._join_and_finish

    def rec_a(*a, **k):
        order.append("a")
        return real_join_a(*a, **k)

    def rec_b(*a, **k):
        order.append("b")
        return real_join_b(*a, **k)
    wa._join_and_finish = rec_a
    wb._join_and_finish = rec_b
    entered = threading.Event()
    release = threading.Event()
    real_a = wa._run_pipeline

    def slow_a(changed, deleted, sr):
        entered.set()
        release.wait(30)
        return real_a(changed, deleted, sr)
    wa._run_pipeline = slow_a
    release_thread = threading.Thread(
        target=lambda: (time.sleep(1.0), release.set()), daemon=True)
    try:
        time.sleep(0.6)  # 基线
        (proj_a / "a.py").write_text("def gate_hold():\n    return 1\n", encoding="utf-8")
        assert entered.wait(15), "a 管线未进入（未持闸）"
        assert wa._holding_gate, "a 未标记持闸"
        release_thread.start()
        registry.stop_all()  # 阻塞；先 join a（信号量持有者），再 join b
        assert order == ["a", "b"], f"停机 join 序非信号量持有者优先: {order}"
    finally:
        release.set()
        registry.stop_all()


def test_mount_after_stop_all_covered_by_atexit(polling, tmp_path):
    """票 02 评审：stop_all 与并发挂载竞态不变式——stop_all 开始后新挂载的 watcher
    不在停机快照内，但 start() 已注册 atexit handler 兜底（进程退出时 stop 它）：
    "被 stop_all 停 或 由 atexit 覆盖"两者必居其一（显式断言 _stopped + atexit 注册）。"""
    import graphify.serve_watcher as W
    root = _mini_proj(tmp_path)
    registry = W.WatcherRegistry(_FakeCache(), poll_interval=0.2)
    wa = registry.mount(root, root / "graphify-out")
    assert wa.is_alive
    registry.stop_all()
    assert not wa.is_alive
    assert registry._stopped, "stop_all 未置 _stopped 标记"
    # stop_all 后并发挂载：不在停机快照内 → 由 atexit 兜底
    wb = registry.mount(root, root / "graphify-out")
    try:
        assert wb.is_alive, "stop_all 后挂载仍应可用（atexit 兜底）"
        assert wb._atexit_registered, "新 watcher 未注册 atexit（停机兜底缺失）"
    finally:
        wb.stop()  # 模拟 atexit 兜底清理
        assert not wb.is_alive


# === 验收 3：graph_stats watched_projects（加性字段，恒存在）=======================

def _init_session(client) -> dict:
    _INIT_BODY = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                   "clientInfo": {"name": "test", "version": "0"}},
    }
    _MCP_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    init = client.post("/mcp", headers=_MCP_HEADERS, json=_INIT_BODY)
    assert init.status_code == 200
    headers = {**_MCP_HEADERS, "mcp-session-id": init.headers.get("mcp-session-id")}
    client.post("/mcp", headers=headers,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    return headers


def _call_tool(client, headers, name, arguments, rid) -> str:
    resp = client.post("/mcp", headers=headers, json={
        "jsonrpc": "2.0", "id": rid, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    })
    assert resp.status_code == 200
    return resp.json()["result"]["content"][0]["text"]


def _parse_meta(text: str) -> dict:
    for line in reversed(text.splitlines()):
        if line.startswith("_meta:"):
            return json.loads(line[len("_meta:"):])
    raise AssertionError(f"响应缺 _meta 行: {text!r}")


def test_graph_stats_watched_projects(polling, fast_watch, tmp_path, monkeypatch):
    """graph_stats 的 _meta 含 watched_projects 数组（root+backend+status）：
    多 watcher 列全；零 watcher（watch 关）恒为 []；正文不变（加性，零破坏既有消费者）。"""
    import graphify.serve as S
    import rebuild_entry
    from starlette.testclient import TestClient
    root = _mini_proj(tmp_path / "proj-a")
    proj_b = _mini_proj(tmp_path / "proj-b")
    rebuild_entry.rebuild(root)
    rebuild_entry.rebuild(proj_b)
    graph_path = str(root / "graphify-out" / "graph.json")
    # 多 watcher：watch 开 → 默认 eager + proj-b 惰性挂载
    monkeypatch.setenv("GRAPHIFY_WATCH", "1")
    app = S._build_http_app(graph_path, json_response=True)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        headers = _init_session(client)
        out = _call_tool(client, headers, "graph_stats",
                         {"project_path": str(proj_b)}, rid=2)
        assert "Nodes: " in out, "正文被破坏（加性字段不应改正文）"
        meta = _parse_meta(out)
        assert "watched_projects" in meta, f"watched_projects 缺失: {meta}"
        wps = meta["watched_projects"]
        roots = {w["project_root"] for w in wps}
        assert roots == {str(Path(root).resolve()), str(Path(proj_b).resolve())}, \
            f"多 watcher 未列全: {wps}"
        for w in wps:
            assert w["backend"] in ("watchdog", "polling"), f"backend 词表异常: {w}"
            assert w["status"] in ("active", "disabled"), f"status 词表异常: {w}"
    # 零 watcher：watch 关 → watched_projects 恒为 []
    monkeypatch.delenv("GRAPHIFY_WATCH", raising=False)
    app2 = S._build_http_app(graph_path, json_response=True)
    with TestClient(app2, base_url="http://127.0.0.1") as client:
        headers = _init_session(client)
        out = _call_tool(client, headers, "graph_stats", {}, rid=1)
        meta = _parse_meta(out)
        assert "watched_projects" in meta, "零 watcher 时字段应存在"
        assert meta["watched_projects"] == [], f"零 watcher 应 []: {meta['watched_projects']}"


# === 验收 4：WatcherRegistry 独立单测——status 汇总（词表统一）======================

def test_registry_status_summary_disabled(polling, tmp_path, monkeypatch):
    """status 词表统一：active/disabled 是状态字段、polling/watchdog 是 backend 字段；
    自禁用 watcher 在 status_summary 中 status=disabled（与 stderr "auto-sync disabled"
    措辞一致）。"""
    import graphify.serve_watcher as W
    import rebuild_entry
    root = _mini_proj(tmp_path)
    rebuild_entry.rebuild(root)
    monkeypatch.setattr(W, "_MAX_SYNC_FAILURE_RETRIES", 2)
    registry = W.WatcherRegistry(_FakeCache(), debounce=0.1, poll_interval=0.2)
    w = registry.mount(root, root / "graphify-out")
    try:
        summary = registry.status_summary()
        assert len(summary) == 1 and summary[0]["status"] == "active"
        assert summary[0]["backend"] in ("watchdog", "polling")
        assert "project_root" in summary[0]
        # 自禁用 → status=disabled
        def boom(changed, deleted, sr):
            raise RuntimeError("boom")
        w._run_pipeline = boom
        time.sleep(0.6)  # 基线
        (root / "a.py").write_text("x = 1\n", encoding="utf-8")
        assert _wait_for(lambda: not w.is_alive, timeout=40), "watcher 未自禁用"
        summary = registry.status_summary()
        assert len(summary) == 1
        assert summary[0]["status"] == "disabled", f"自禁用 watcher 应 status=disabled: {summary}"
    finally:
        registry.stop_all()


# === 验收 5：LRU 逐出重入 = 新挂载周期 → 补齐一次（跨票组合语义）====================

def test_lru_evict_reentry_backfills_again(polling, tmp_path):
    """LRU 逐出重入 = 新挂载周期 → 补齐一次（票 03 逐出 × 票 04 补齐；票 04 只测了
    dead 替换周期，本票补逐出周期）。"""
    import graphify.serve_watcher as W
    import rebuild_entry
    root = _mini_proj(tmp_path)
    rebuild_entry.rebuild(root)
    (root / "a.py").write_text(
        "import b\n\ndef foo():\n    return b.bar()\n\ndef reentry1():\n    return 1\n",
        encoding="utf-8")
    assert "reentry1()" not in _labels(root / "graphify-out"), "前置：图未含新语料"
    registry = W.WatcherRegistry(_FakeCache(), debounce=0.1, poll_interval=0.2)
    w1 = registry.mount(root, root / "graphify-out")
    calls1 = {"n": 0}
    real1 = w1._run_pipeline

    def rec1(changed, deleted, sr):
        calls1["n"] += 1
        return real1(changed, deleted, sr)
    w1._run_pipeline = rec1
    try:
        assert _wait_for(lambda: "reentry1()" in _labels(root / "graphify-out"), timeout=40), \
            "首挂补齐未完成"
        assert calls1["n"] >= 1, "首挂未触发补齐"
        # LRU 逐出 → watcher 停止并从 registry 移除
        registry.evict_graph(str((root / "graphify-out" / "graph.json").resolve()))
        assert not w1.is_alive, "逐出后 watcher 未停止"
        assert registry.get(root) is None, "逐出后未从 registry 移除"
        # 新语料又陈旧 → 重挂 = 新挂载周期 → 再补齐一次
        (root / "a.py").write_text(
            "import b\n\ndef foo():\n    return b.bar()\n\ndef reentry1():\n    return 1\n"
            "\ndef reentry2():\n    return 2\n",
            encoding="utf-8")
        w2 = registry.mount(root, root / "graphify-out")
        assert w2 is not w1 and w2.is_alive, "重挂未产生全新 watcher"
        calls2 = {"n": 0}
        real2 = w2._run_pipeline

        def rec2(changed, deleted, sr):
            calls2["n"] += 1
            return real2(changed, deleted, sr)
        w2._run_pipeline = rec2
        assert _wait_for(lambda: "reentry2()" in _labels(root / "graphify-out"), timeout=40), \
            "逐出重入（新挂载周期）未再补齐"
        assert calls2["n"] >= 1, "逐出重入未触发补齐"
    finally:
        registry.stop_all()


# === 验收 6：默认 pinned 自禁用防回归（controller 交接）=============================

def test_serve_pinned_default_self_disable_not_revived_by_default_query(polling, fast_watch,
                                                                        tmp_path, monkeypatch):
    """默认项目 pinned watcher 自禁用后，默认图查询（project_path=None）不复活（惰性挂载
    仅对非空 project_path 触发——死亡直到重启）；显式 project_path=默认根走 dead-replace
    复活（合法多项目用法）。"""
    import graphify.serve as S
    import graphify.serve_watcher as W
    import rebuild_entry
    root = _mini_proj(tmp_path)
    rebuild_entry.rebuild(root)
    monkeypatch.setattr(W, "_MAX_SYNC_FAILURE_RETRIES", 2)
    server = S._build_server(str(root / "graphify-out" / "graph.json"), watch=True)
    registry = server._graphify_registry
    try:
        wd = registry.get(root)
        assert wd is not None and wd.is_alive, "默认 watcher 未挂载"

        def boom(changed, deleted, sr):
            raise RuntimeError("boom")
        wd._run_pipeline = boom
        time.sleep(0.6)  # 基线
        (root / "a.py").write_text("x = 1\n", encoding="utf-8")
        assert _wait_for(lambda: not wd.is_alive, timeout=40), "默认 watcher 未自禁用"
        # 默认图查询（project_path=None）→ _select_graph(None) 不触发惰性挂载
        server._graphify_select_graph(None)
        assert registry.get(root) is wd, "默认图查询替换了 watcher（不应挂载）"
        assert not wd.is_alive, "默认图查询复活了 dead watcher（违反死亡直到重启）"
        # 显式 project_path=默认根 → dead-replace 复活（合法多项目用法）
        server._graphify_select_graph(str(root))
        wd2 = registry.get(root)
        assert wd2 is not None and wd2 is not wd, "显式 project_path 未 dead-replace 复活"
        assert wd2.is_alive, "复活后的 watcher 未存活"
    finally:
        registry.stop_all()


# === 验收 7：observer 泄漏修复（真实 watchdog + 确定性双路）=========================

def test_self_disable_early_stop_stops_observer_deterministic(tmp_path):
    """确定性（无真 watchdog）：自禁用后 stop() 走早退分支也必须停/join observer。
    用 fake observer 注入——早退分支是"线程死了但 observer 可能活着"的统一咽喉。"""
    import graphify.serve_watcher as W
    root = _mini_proj(tmp_path)

    class FakeObserver:
        def __init__(self):
            self.stop_calls = 0
            self.join_calls = 0

        def stop(self):
            self.stop_calls += 1

        def join(self, timeout=None):
            self.join_calls += 1

    watcher = W.ServeWatcher(root)
    fake = FakeObserver()
    watcher._observer = fake
    watcher._running = False  # 模拟自禁用后状态（_run_loop finally 已清 _running）
    watcher.stop()
    assert fake.stop_calls == 1, "早退分支未停 observer（泄漏）"
    assert fake.join_calls == 1, "早退分支未 join observer（泄漏）"


def test_watchdog_mode_self_disable_stops_observer(tmp_path, monkeypatch):
    """真实 watchdog 后端回归：自禁用（auto-sync disabled）后 stop() 必须停/join
    observer（修复 observer 线程泄漏——polling fixture 掩盖的 bug：自禁用退出循环不
    停 observer，stop() 早退分支也够不着 observer）。watchdog 未安装则跳过（软依赖）。"""
    import graphify.serve_watcher as W
    if W._WatchdogObserver is None:
        pytest.skip("watchdog 未安装（软依赖缺失）")
    import rebuild_entry
    root = _mini_proj(tmp_path)
    rebuild_entry.rebuild(root)
    monkeypatch.setattr(W, "_MAX_SYNC_FAILURE_RETRIES", 2)
    watcher = W.ServeWatcher(root, debounce=0.3)
    watcher.start()
    observer = watcher._observer
    assert observer is not None, "watchdog 模式下 observer 未启动"

    def boom(changed, deleted, sr):
        raise RuntimeError("boom")
    watcher._run_pipeline = boom
    try:
        time.sleep(0.5)  # 等基线
        (root / "a.py").write_text("x = 1\n", encoding="utf-8")
        assert _wait_for(lambda: not watcher.is_alive, timeout=40), "自禁用未发生"
        watcher.stop()  # 修复前：早退分支不碰 observer → 线程泄漏
        assert not observer.is_alive, "observer 线程泄漏（自禁用后未停/join）"
    finally:
        watcher.stop()


# === 验收 8：FB3 耗尽路径——final flush 重试耗尽诚实告警 =============================

def test_final_flush_retries_exhausted_warns(polling, tmp_path, capsys):
    """3 次 final-flush 重试全失败（hook 全程持锁）→ 诚实告警 "final flush skipped"
    + pending 计数 + 锁竞争单行日志；stop() 正常返回、有界耗时（~3s，不无界等待）。"""
    import graphify.serve_watcher as W
    import rebuild_entry
    root = _mini_proj(tmp_path)
    rebuild_entry.rebuild(root)
    assert rl._acquire_lock(root) is True, "测试前置：hook 未持到锁"
    watcher = W.ServeWatcher(root, out_dir=root / "graphify-out",
                             poll_interval=0.2, debounce=10.0)
    watcher.start()
    try:
        time.sleep(0.6)  # 基线
        (root / "a.py").write_text(
            "import b\n\ndef foo():\n    return b.bar()\n\ndef exhausted_sym():\n    return 7\n",
            encoding="utf-8")
        time.sleep(0.5)  # 编辑进 pending
        t0 = time.time()
        watcher.stop()  # 锁全程持有 → 3 次重试全失败 → 告警
        elapsed = time.time() - t0
        assert elapsed < 15, f"停机异常耗时: {elapsed:.1f}s"
        err = capsys.readouterr().err
        assert "final flush skipped" in err, f"锁耗尽未告警: {err!r}"
        assert "rebuild lock still busy after 3 retries" in err, f"告警未说明重试次数: {err!r}"
        assert "1 changed" in err, f"告警未含 pending 计数: {err!r}"
        assert "rebuild lock busy on" in err, f"锁竞争单行日志缺失: {err!r}"
    finally:
        watcher.stop()
        _cleanup(root)


# === 验收 9：挂载补齐 merged batch freshness 标注（周期标志）=========================

def test_backfill_cycle_flag_marks_merged_batch(tmp_path):
    """freshness 修复：merged batch（补齐 + 立即编辑，非空）必须标注 rebuilding——
    backfill 判定用周期标志而非空批次检测（watchdog 模式 merged batch 的确定性复现：
    直接投递编辑进 pending，绕过循环时序）。"""
    import graphify.serve_watcher as W
    import rebuild_entry
    root = _mini_proj(tmp_path)
    rebuild_entry.rebuild(root)
    watcher = W.ServeWatcher(root, out_dir=root / "graphify-out",
                             poll_interval=0.2, debounce=0.1)
    watcher._enqueue_backfill()  # 模拟挂载补齐入队（周期标志置位）
    watcher._record(root / "a.py", deleted=False)  # 模拟 watchdog 事件（merged，非空）
    assert watcher._backfill_cycle, "前置：挂载周期标志未设置"
    changed, deleted = watcher._take_batch()
    assert changed == [root / "a.py"], f"前置：merged batch 应含编辑: {changed}"
    slow_started = threading.Event()
    release = threading.Event()
    real = watcher._run_pipeline

    def slow(ch, dl, sr):
        slow_started.set()
        release.wait(30)
        return real(ch, dl, sr)
    watcher._run_pipeline = slow
    result = {}

    def run_flush():
        result["r"] = watcher._flush_batch(changed, deleted)
    t = threading.Thread(target=run_flush, daemon=True)
    t.start()
    try:
        assert slow_started.wait(15), "flush 未进入管线"
        state = root / "graphify-out" / ".rebuild-state.json"
        d = json.loads(state.read_text(encoding="utf-8"))
        assert d["phase"] == "rebuilding", f"merged batch 未标注 rebuilding: {d}"
    finally:
        release.set()
        t.join(30)
        watcher.stop()
        _cleanup(root)


# === 验收 10：stderr 日志全景（沿 Task 10 风格）====================================

def test_stderr_log_panorama(polling, tmp_path, monkeypatch, capsys):
    """stderr 日志全景：挂载/补齐入队/上限逐出/LRU 逐出/自禁用/复活均有单行日志。"""
    import graphify.serve_watcher as W
    monkeypatch.setenv("GRAPHIFY_MAX_WATCHERS", "1")
    monkeypatch.setattr(W, "_MAX_SYNC_FAILURE_RETRIES", 2)
    root = _mini_proj(tmp_path / "proj-a")
    proj_b = _mini_proj(tmp_path / "proj-b")
    proj_c = _mini_proj(tmp_path / "proj-c")
    registry = W.WatcherRegistry(_FakeCache(), debounce=0.1, poll_interval=0.2)
    registry.mount(root, root / "graphify-out")               # 挂载 + 补齐入队
    wb = registry.mount(proj_b, proj_b / "graphify-out")      # 上限=1 → 上限逐出 root
    assert not registry.get(root), "上限逐出未生效"
    registry.evict_graph(str((proj_b / "graphify-out" / "graph.json").resolve()))  # LRU 逐出
    wc = registry.mount(proj_c, proj_c / "graphify-out")

    def boom(changed, deleted, sr):
        raise RuntimeError("boom")
    wc._run_pipeline = boom
    time.sleep(0.6)  # 基线
    (proj_c / "a.py").write_text("x = 1\n", encoding="utf-8")
    assert _wait_for(lambda: not wc.is_alive, timeout=40), "proj-c 未自禁用"
    registry.mount(proj_c, proj_c / "graphify-out")  # dead-replace 复活
    err = capsys.readouterr().err
    for needle in ("watching ", "backfill enqueued", "cap eviction", "evicted watcher",
                   "auto-sync disabled", "replaced dead watcher"):
        assert needle in err, f"日志缺失 [{needle}]: {err!r}"
    wb.stop()  # 清理 wb（已逐出但线程可能还在收尾）
    registry.stop_all()
