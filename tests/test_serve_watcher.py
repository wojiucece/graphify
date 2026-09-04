"""Task 10 serve 内置 watcher 测试（spec Seam 4 触发链：watcher 端到端）。

文件变化 → 产物更新 → 热重载（含删除/重命名/优雅停机用例）。watch 重建触发测试先例：
tests/test_watch_rebuild_trigger.py（路由级）；本文件补 watcher 全链路。

关键语义（架构票 04 三条铁律）：
- 删除语义：pending 删除集 + extraction 剔除 + seed 修剪 + extract cache 失效 + 兜底过滤
- graceful shutdown：stop() 阻塞等待当前批次完成（无半写）
- 重建期间查询走旧图（mtime 键控缓存），完成后 on_pipeline_complete 直通失效原子换图
- 默认关：serve 无 --watch/GRAPHIFY_WATCH 时零 watcher、零 import 副作用
"""
import json
import sys
import threading
import time
from pathlib import Path

import pytest


def _mini_proj(tmp_path: Path) -> Path:
    (tmp_path / "a.py").write_text(
        "import b\n\ndef foo():\n    return b.bar()\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("def bar():\n    return 1\n", encoding="utf-8")
    return tmp_path


def _wait_for(pred, timeout: float = 30.0, interval: float = 0.1) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return pred()


def _graph_nodes(root: Path) -> dict:
    g = root / "graphify-out" / "graph.json"
    if not g.exists():
        return {}
    data = json.loads(g.read_text(encoding="utf-8"))
    return {n["label"]: n for n in data.get("nodes", [])}


def _labels(root: Path) -> set:
    return set(_graph_nodes(root))


def _wait_for_fts_hit(root: Path, term: str, timeout: float = 15.0) -> bool:
    """FTS 检索重试探针：容忍 watcher 线程并发原子替换 .fts-index.db 的瞬时
    sqlite/PermissionError（Windows 文件锁），也容忍旧缓存未重投影的窗口。"""
    from fts_cache import fts_search, open_readonly

    db = root / "graphify-out" / ".fts-index.db"

    def probe():
        try:
            conn = open_readonly(db)
            try:
                _, hits = fts_search(conn, [term])
                return hits >= 1
            finally:
                conn.close()
        except Exception:
            return False

    return _wait_for(probe, timeout=timeout, interval=0.2)


@pytest.fixture
def polling(monkeypatch):
    """强制降级轮询（无 watchdog 语义，可确定性测试防抖/删除/停机）。"""
    import graphify.serve_watcher as W
    monkeypatch.setattr(W, "_WatchdogObserver", None)
    monkeypatch.setattr(W, "_FSHandler", None)


# === 默认关 / 挂载点（serve.py）================================================

def test_build_server_watch_default_off(tmp_path):
    """默认关：无 --watch/GRAPHIFY_WATCH 时 server 无 watcher，且 serve_watcher 不 import."""
    import graphify.serve as S
    root = _mini_proj(tmp_path)
    import rebuild_entry
    rebuild_entry.rebuild(root)
    server = S._build_server(str(root / "graphify-out" / "graph.json"))
    assert getattr(server, "_graphify_watcher", None) is None
    assert "graphify.serve_watcher" not in sys.modules, "默认关不该 import serve_watcher"


def test_build_server_watch_flag_enables(tmp_path):
    """--watch（watch=True）显式开启：watcher 挂上并启动。"""
    import graphify.serve as S
    root = _mini_proj(tmp_path)
    import rebuild_entry
    rebuild_entry.rebuild(root)
    server = S._build_server(str(root / "graphify-out" / "graph.json"), watch=True)
    watcher = getattr(server, "_graphify_watcher", None)
    assert watcher is not None
    try:
        assert watcher.backend_name in ("watchdog", "polling")
    finally:
        watcher.stop()


def test_build_server_watch_env_enables(tmp_path, monkeypatch):
    """GRAPHIFY_WATCH=1 env 显式开启（架构票 04：env 是授权开关之一）。"""
    import graphify.serve as S
    root = _mini_proj(tmp_path)
    import rebuild_entry
    rebuild_entry.rebuild(root)
    monkeypatch.setenv("GRAPHIFY_WATCH", "1")
    server = S._build_server(str(root / "graphify-out" / "graph.json"))
    watcher = getattr(server, "_graphify_watcher", None)
    assert watcher is not None
    try:
        watcher.stop()
    finally:
        monkeypatch.delenv("GRAPHIFY_WATCH", raising=False)


# === 触发链端到端（Seam 4）：编辑 → 产物对更新 → 查询命中新图 =====================

def test_edit_updates_graph_and_fts(polling, tmp_path):
    """改一个文件 → 防抖聚合 → 产物对更新 → 下一次查询命中新图。"""
    import graphify.serve_watcher as W
    root = _mini_proj(tmp_path)
    watcher = W.ServeWatcher(root, poll_interval=0.2, debounce=0.1)
    watcher.start()
    try:
        assert watcher.backend_name == "polling"
        time.sleep(0.6)  # 等基线快照建立（首次扫描只建基线）
        (root / "a.py").write_text(
            "import b\n\ndef foo():\n    return b.bar()\n\ndef baz():\n    return 2\n",
            encoding="utf-8")
        assert _wait_for(lambda: "baz()" in _labels(root)), "graph.json 未更新出新符号"
        # FTS 重投影（05 接口）：缓存可查新符号（重试容忍并发原子替换的瞬时锁）
        assert _wait_for_fts_hit(root, "baz"), "FTS 未命中新符号 baz"
    finally:
        watcher.stop()


def test_edit_after_baseline_rebuild(polling, tmp_path):
    """基线由 rebuild_entry 建立后再编辑，watcher 仍正确更新（防基线竞态）。"""
    import graphify.serve_watcher as W
    root = _mini_proj(tmp_path)
    import rebuild_entry
    rebuild_entry.rebuild(root)
    assert "foo()" in _labels(root)
    watcher = W.ServeWatcher(root, poll_interval=0.2, debounce=0.1)
    watcher.start()
    try:
        time.sleep(0.6)
        (root / "b.py").write_text("def bar():\n    return 1\n\ndef qux():\n    return 3\n",
                                   encoding="utf-8")
        assert _wait_for(lambda: "qux()" in _labels(root))
    finally:
        watcher.stop()


# === 删除/重命名：亡灵节点消失 ==================================================

def test_delete_removes_zombie_nodes(polling, tmp_path):
    """删除文件后，产物中该文件的符号与边消失（无亡灵节点）。"""
    import graphify.serve_watcher as W
    root = _mini_proj(tmp_path)
    import rebuild_entry
    rebuild_entry.rebuild(root)
    assert "foo()" in _labels(root)
    watcher = W.ServeWatcher(root, poll_interval=0.2, debounce=0.1)
    watcher.start()
    try:
        time.sleep(0.6)
        (root / "a.py").unlink()
        assert _wait_for(lambda: "foo()" not in _labels(root)), "亡灵石未消失"
        data = json.loads((root / "graphify-out" / "graph.json").read_text(encoding="utf-8"))
        assert not any(n.get("source_file") == "a.py" for n in data["nodes"]), \
            "删除后仍残留 a.py 节点"
        assert not any(e.get("source_file") == "a.py" for e in data.get("links", [])), \
            "删除后仍残留 a.py 边"
    finally:
        watcher.stop()


def test_rename_equals_delete_create(polling, tmp_path):
    """重命名等价于删旧建新：旧文件符号/边消失，新文件符号进图。"""
    import graphify.serve_watcher as W
    root = _mini_proj(tmp_path)
    import rebuild_entry
    rebuild_entry.rebuild(root)
    watcher = W.ServeWatcher(root, poll_interval=0.2, debounce=0.1)
    watcher.start()
    try:
        time.sleep(0.6)
        (root / "a.py").rename(root / "a2.py")

        def renamed():
            data = json.loads((root / "graphify-out" / "graph.json").read_text(encoding="utf-8"))
            sfs = {n.get("source_file") for n in data["nodes"]}
            return "a.py" not in sfs and "a2.py" in sfs

        assert _wait_for(renamed), "重命名后旧 source_file 未消失或新文件未进图"
    finally:
        watcher.stop()


def test_same_path_delete_create_net_change(polling, tmp_path):
    """同路径 delete+create（原子保存编辑器）→ 净为 changed 而非 deleted。"""
    import graphify.serve_watcher as W
    root = _mini_proj(tmp_path)
    watcher = W.ServeWatcher(root)
    watcher._record(root / "a.py", deleted=True)
    watcher._record(root / "a.py", deleted=False)
    changed, deleted = watcher._take_batch()
    assert root / "a.py" in changed
    assert root / "a.py" not in deleted


def test_moved_event_splits_delete_and_create(tmp_path):
    """MovedEvent 拆 delete+create（架构票铁律 1：重命名=删旧建新）。"""
    import graphify.serve_watcher as W
    root = _mini_proj(tmp_path)
    watcher = W.ServeWatcher(root)
    watcher._record(root / "a.py", deleted=True)    # MovedEvent.src_path
    watcher._record(root / "a2.py", deleted=False)  # MovedEvent.dest_path
    changed, deleted = watcher._take_batch()
    assert root / "a2.py" in changed
    assert root / "a.py" in deleted


def test_filter_deleted_prunes_extraction(tmp_path):
    """删除过滤直接单元测试：extraction nodes/edges 剔除 source_file ∈ 删除集。"""
    import graphify.serve_watcher as W
    root = tmp_path
    watcher = W.ServeWatcher(root)
    extraction = {
        "nodes": [
            {"id": "function:1", "label": "foo", "source_file": "a.py"},
            {"id": "function:2", "label": "bar", "source_file": "b.py"},
        ],
        "edges": [
            {"source": "function:1", "target": "file:a.py", "source_file": "a.py"},
        ],
        "failed_refs": [],
    }
    filtered = watcher._filter_deleted(extraction, [root / "a.py"])
    labels = {n["label"] for n in filtered["nodes"]}
    assert labels == {"bar"}
    assert filtered["edges"] == []


def test_seed_pruned_for_deleted_file(tmp_path):
    """semantic-seed.json 中引用已删文件的节点/边被修剪写回（防下轮复活）。"""
    import graphify.serve_watcher as W
    root = tmp_path
    out = root / "graphify-out"
    out.mkdir()
    seed_path = out / "semantic-seed.json"
    seed = {
        "nodes": [
            {"id": "document:1", "label": "a 笔记", "source_file": "a.md", "_origin": "semantic"},
            {"id": "concept:2", "label": "b 概念", "source_file": "b.md", "_origin": "semantic"},
        ],
        "edges": [{"source": "document:1", "target": "file:a.py", "source_file": "a.md"}],
        "hyperedges": [],
    }
    seed_path.write_text(json.dumps(seed), encoding="utf-8")
    watcher = W.ServeWatcher(root, out_dir=out)
    watcher._prune_seed_for_deleted([root / "a.md"])
    updated = json.loads(seed_path.read_text(encoding="utf-8"))
    assert all(n.get("source_file") != "a.md" for n in updated["nodes"])
    assert all(e.get("source_file") != "a.md" for e in updated["edges"])
    assert any(n.get("source_file") == "b.md" for n in updated["nodes"])


def test_graphify_out_writes_do_not_retrigger(polling, tmp_path):
    """graphify-out 自写不触发重建（防无限循环）：无外部事件时 pipeline 调用数恒 0。"""
    import graphify.serve_watcher as W
    root = _mini_proj(tmp_path)
    import rebuild_entry
    rebuild_entry.rebuild(root)
    watcher = W.ServeWatcher(root, poll_interval=0.2, debounce=0.1)
    calls = {"n": 0}
    real = watcher._run_pipeline

    def recorder(changed, deleted, sr):
        calls["n"] += 1
        return real(changed, deleted, sr)
    watcher._run_pipeline = recorder
    watcher.start()
    try:
        time.sleep(2.0)  # 跨多个轮询周期
        assert calls["n"] == 0, f"graphify-out 自写触发了重建: {calls}"
    finally:
        watcher.stop()


# === 优雅停机（铁律 2）=========================================================

def test_stop_blocks_until_batch_complete_no_half_write(polling, tmp_path):
    """stop() 阻塞等待当前批次完成；落盘文件完整（无半写）。"""
    import graphify.serve_watcher as W
    root = _mini_proj(tmp_path)
    import rebuild_entry
    rebuild_entry.rebuild(root)
    watcher = W.ServeWatcher(root, poll_interval=0.2, debounce=0.1)
    entered = threading.Event()
    done = threading.Event()
    real = watcher._run_pipeline

    def slow_pipeline(changed, deleted, sr):
        entered.set()
        time.sleep(1.0)
        try:
            return real(changed, deleted, sr)
        finally:
            done.set()
    watcher._run_pipeline = slow_pipeline
    watcher.start()
    try:
        time.sleep(0.6)  # 等基线快照（首次扫描只建基线）
        (root / "a.py").write_text("x = 1\n", encoding="utf-8")  # 触发批次
        assert entered.wait(15), "pipeline 未进入"
        t0 = time.time()
        watcher.stop()
        elapsed = time.time() - t0
        assert done.is_set(), "stop 返回时批次尚未完成"
        assert elapsed >= 0.9, f"stop 未阻塞等待批次完成: {elapsed:.2f}s"
        # 无半写：graph.json 合法 JSON 且完整
        data = json.loads((root / "graphify-out" / "graph.json").read_text(encoding="utf-8"))
        assert "nodes" in data
    finally:
        watcher.stop()


def test_stop_flushes_pending_batch(polling, tmp_path):
    """stop() 时尚未启动的 pending 批次也被 flush（不丢事件）。"""
    import graphify.serve_watcher as W
    root = _mini_proj(tmp_path)
    import rebuild_entry
    rebuild_entry.rebuild(root)
    watcher = W.ServeWatcher(root, poll_interval=0.2, debounce=10.0)  # 事件入 pending，防抖未到
    calls = {"n": 0}
    real = watcher._run_pipeline

    def recorder(changed, deleted, sr):
        calls["n"] += 1
        return real(changed, deleted, sr)
    watcher._run_pipeline = recorder
    watcher.start()
    try:
        time.sleep(0.6)  # 基线
        (root / "b.py").write_text("def bar():\n    return 1\n\ndef zap():\n    return 4\n",
                                   encoding="utf-8")
        time.sleep(0.5)  # 让事件进入 pending（debounce 未到，不 flush）
        watcher.stop()   # 应 flush 一次 pending 批次
        assert calls["n"] >= 1, "stop 未 flush pending 批次"
        assert "zap()" in _labels(root)
    finally:
        watcher.stop()


# === 失败退避与上限（复用 watch.py 已移植常量语义）==============================

def test_backoff_retries_same_batch_and_recovers(polling, tmp_path):
    """失败退避：首败后重试同一批，恢复后成功落地。"""
    import graphify.serve_watcher as W
    root = _mini_proj(tmp_path)
    import rebuild_entry
    rebuild_entry.rebuild(root)
    watcher = W.ServeWatcher(root, poll_interval=0.2, debounce=0.1)
    calls = {"n": 0}
    real = watcher._run_pipeline

    def flaky(changed, deleted, sr):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient failure")
        return real(changed, deleted, sr)
    watcher._run_pipeline = flaky
    watcher.start()
    try:
        time.sleep(0.6)
        (root / "b.py").write_text("def bar():\n    return 1\n\ndef recover_sym():\n    return 5\n",
                                   encoding="utf-8")
        assert _wait_for(lambda: "recover_sym()" in _labels(root)), "退避重试未恢复"
        assert calls["n"] >= 2, f"首败后未重试同一批: {calls}"
    finally:
        watcher.stop()


def test_backoff_disables_after_repeated_failures(polling, tmp_path, monkeypatch, capsys):
    """连续失败超上限 → 退避至封顶后自动停用 auto-sync（复用 _MAX_SYNC_FAILURE_RETRIES）。"""
    import graphify.serve_watcher as W
    monkeypatch.setattr(W, "_MAX_SYNC_FAILURE_RETRIES", 2)
    root = _mini_proj(tmp_path)
    watcher = W.ServeWatcher(root, poll_interval=0.2, debounce=0.1)

    def boom(changed, deleted, sr):
        raise RuntimeError("boom")
    watcher._run_pipeline = boom
    watcher.start()
    try:
        time.sleep(0.6)  # 等基线快照
        (root / "a.py").write_text("x = 1\n", encoding="utf-8")
        assert _wait_for(lambda: not watcher._running, timeout=40), "连续失败后未停用"
        err = capsys.readouterr().err
        assert "auto-sync disabled" in err, f"未输出停用日志: {err}"
        assert "retrying" in err, f"未输出退避日志: {err}"
    finally:
        watcher.stop()


# === 防抖聚合 ==================================================================

def test_debounce_aggregates_multiple_changes(polling, tmp_path):
    """防抖窗口内多个变化聚合为一批次。"""
    import graphify.serve_watcher as W
    root = _mini_proj(tmp_path)
    watcher = W.ServeWatcher(root, poll_interval=0.2, debounce=0.1)
    batches = []
    real = watcher._run_pipeline

    def recorder(changed, deleted, sr):
        batches.append((sorted(str(p.name) for p in changed), sorted(str(p.name) for p in deleted)))
        return real(changed, deleted, sr)
    watcher._run_pipeline = recorder
    watcher.start()
    try:
        time.sleep(0.6)  # 基线
        (root / "a.py").write_text("def one():\n    return 1\n", encoding="utf-8")
        (root / "b.py").write_text("def two():\n    return 2\n", encoding="utf-8")
        assert _wait_for(lambda: len(batches) >= 1), "无批次执行"
        assert len(batches[0][0]) == 2, f"防抖窗未聚合两文件: {batches}"
    finally:
        watcher.stop()


# === serve 缓存直通失效（Task 06 _GraphContextCache）===========================

def test_graph_context_cache_invalidate_atomic_swap(tmp_path):
    """watcher 重建后 invalidate → 下次 load 原子换到新图（重建期间旧图不阻断）。"""
    import graphify.serve as S
    root = _mini_proj(tmp_path)
    import rebuild_entry
    rebuild_entry.rebuild(root)
    cache = S._GraphContextCache(8)
    graph_path = str(root / "graphify-out" / "graph.json")
    G0, _ = cache.load(graph_path, pinned=True)
    labels0 = {d.get("label") for _, d in G0.nodes(data=True)}
    assert "foo()" in labels0 and "baz()" not in labels0
    # watcher 侧重建（事实层被原子替换，mtime/size 指纹变化）
    (root / "a.py").write_text(
        "import b\n\ndef foo():\n    return b.bar()\n\ndef baz():\n    return 2\n",
        encoding="utf-8")
    import rebuild_entry as R
    R.rebuild(root)
    # on_pipeline_complete 回调直通失效 → 下次 load 原子换新图
    # （mtime 键控缓存本身会在指纹变化后重读；invalidate 保证换图确定性）
    cache.invalidate(graph_path)
    G1, _ = cache.load(graph_path, pinned=True)
    labels1 = {d.get("label") for _, d in G1.nodes(data=True)}
    assert "baz()" in labels1, "invalidate 后未换到新图"
    # 文件未变时 load 命中缓存不重读（重建期间查询走旧图的机制：键不变不换图）
    G_hit, _ = cache.load(graph_path, pinned=True)
    assert G_hit is G1, "未变化的图应命中缓存"


def test_mount_watcher_on_complete_invalidates_cache(tmp_path):
    """mount_watcher 的 on_pipeline_complete 回调 = _GraphContextCache.invalidate。"""
    import graphify.serve_watcher as W
    root = _mini_proj(tmp_path)
    import rebuild_entry
    rebuild_entry.rebuild(root)

    class FakeCache:
        def __init__(self):
            self.invalidated = []

        def invalidate(self, path):
            self.invalidated.append(path)

    fake = FakeCache()
    watcher = W.mount_watcher(str(root / "graphify-out" / "graph.json"), fake, watch=True)
    assert watcher is not None
    try:
        watcher._on_complete()
        assert fake.invalidated == [str((root / "graphify-out" / "graph.json").resolve())]
    finally:
        watcher.stop()


def test_mount_watcher_disabled_returns_none(tmp_path, monkeypatch):
    """未开启（watch=None + 无 env）→ mount_watcher 返回 None（零副作用）。"""
    import graphify.serve_watcher as W
    monkeypatch.delenv("GRAPHIFY_WATCH", raising=False)
    assert W.mount_watcher(str(tmp_path / "graphify-out" / "graph.json"), object()) is None


def test_mount_watcher_env_enables(tmp_path, monkeypatch):
    """env GRAPHIFY_WATCH=1 → mount_watcher 开启。"""
    import graphify.serve_watcher as W
    monkeypatch.setenv("GRAPHIFY_WATCH", "1")
    watcher = W.mount_watcher(str(tmp_path / "graphify-out" / "graph.json"), object())
    assert watcher is not None
    try:
        watcher.stop()
    finally:
        monkeypatch.delenv("GRAPHIFY_WATCH", raising=False)


# === watchdog 真后端（软依赖存在时）=============================================

def test_watchdog_mode_e2e_edit(tmp_path):
    """watchdog Observer 真后端端到端：保存文件 → 秒级产物更新（软依赖存在时）。"""
    import graphify.serve_watcher as W
    if W._WatchdogObserver is None:
        pytest.skip("watchdog 未安装（软依赖缺失）")
    root = _mini_proj(tmp_path)
    import rebuild_entry
    rebuild_entry.rebuild(root)
    watcher = W.ServeWatcher(root, debounce=0.3)
    assert watcher.backend_name == "watchdog"
    watcher.start()
    try:
        (root / "a.py").write_text(
            "import b\n\ndef foo():\n    return b.bar()\n\ndef wd_sym():\n    return 9\n",
            encoding="utf-8")
        assert _wait_for(lambda: "wd_sym()" in _labels(root), timeout=40), \
            "watchdog 事件未驱动重建"
    finally:
        watcher.stop()


def test_watchdog_mode_delete_removes_zombie(tmp_path):
    """watchdog 真后端删除语义：删除文件 → 亡灵节点消失。"""
    import graphify.serve_watcher as W
    if W._WatchdogObserver is None:
        pytest.skip("watchdog 未安装（软依赖缺失）")
    root = _mini_proj(tmp_path)
    import rebuild_entry
    rebuild_entry.rebuild(root)
    watcher = W.ServeWatcher(root, debounce=0.3)
    watcher.start()
    try:
        time.sleep(0.5)
        (root / "a.py").unlink()
        assert _wait_for(lambda: "foo()" not in _labels(root), timeout=40), \
            "watchdog 删除事件未清亡灵节点"
    finally:
        watcher.stop()
