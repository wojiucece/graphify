"""serve-memory 票 03：R1 补齐门控——新鲜项目重挂零重建，陈旧（改/增/删）仍修复。

spec: docs/specs/serve-memory-spec.md §R1（:71-81）+ §Testing 验收 7/8（:104-105）。

覆盖（issue checklist）：
- 门控五路（验收 7）：新鲜跳过（零重建）/ 真实陈旧执行 / touch 误报执行（文档化容忍）/
  删除触发（count 捕获）/ 扫描超时回退无条件
- mixed batch 不门控：挂载后 debounce 窗内编辑 → 直接重建（不吞编辑，显式断言）
- 锁 owner：watcher 重建后状态文件 source_count 已更新（锁内写入生效——锁外调用
  静默丢 count 是实施红线）
- 逃生口 GRAPHIFY_BACKFILL=always 恢复无条件补齐（US8 回滚旋钮）
- 既有 stale 活证测试（test_mount_backfill_lock.py test_lazy_mount_backfills_stale_graph
  与 self-heal E2E）零改动保留——有真实陈旧，门控正确性活证
"""
import json
import os
import time
from pathlib import Path

import pytest


def _mini_proj(tmp_path: Path) -> Path:
    """mini 项目（2 个 Python 文件）；返回 resolve() 归一 root。"""
    root = Path(tmp_path).resolve()
    root.mkdir(parents=True, exist_ok=True)
    (root / "a.py").write_text(
        "import b\n\ndef foo():\n    return b.bar()\n", encoding="utf-8")
    (root / "b.py").write_text("def bar():\n    return 1\n", encoding="utf-8")
    return root


def _wait_for(pred, timeout: float = 30.0, interval: float = 0.1) -> bool:
    """轮询直至 pred() 为真。容忍 watcher 线程并发原子替换 graph.json 的瞬时
    Windows 文件锁（PermissionError ⊂ OSError）与状态文件写中读（JSONDecodeError
    ⊂ ValueError，_write_state 非原子直写）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if pred():
                return True
        except (OSError, ValueError):
            pass  # 瞬时锁窗口 / 状态文件写中读，重试
        time.sleep(interval)
    try:
        return bool(pred())
    except (OSError, ValueError):
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
    """强制降级轮询（无 watchdog 语义，可确定性测试防抖/门控时序）。"""
    import graphify.serve_watcher as W
    monkeypatch.setattr(W, "_WatchdogObserver", None)
    monkeypatch.setattr(W, "_FSHandler", None)


@pytest.fixture
def fast_watch(monkeypatch):
    """短防抖/快轮询（registry 构造时读模块常量，需先 monkeypatch）。"""
    import graphify.serve_watcher as W
    monkeypatch.setattr(W, "DEFAULT_DEBOUNCE", 0.1)
    monkeypatch.setattr(W, "DEFAULT_POLL_INTERVAL", 0.2)


def _mount_and_count(registry, root, out_dir):
    """挂载并包一层计数（_flush_batch 调用次数 + _run_pipeline 调用次数）。"""
    import graphify.serve_watcher as W
    w = registry.mount(root, out_dir)
    stats = {"flush": 0, "pipeline": 0}
    real_flush = w._flush_batch
    real_pipe = w._run_pipeline

    def rec_flush(changed, deleted, **kw):
        stats["flush"] += 1
        return real_flush(changed, deleted, **kw)
    w._flush_batch = rec_flush

    def rec_pipe(changed, deleted, sr):
        stats["pipeline"] += 1
        return real_pipe(changed, deleted, sr)
    w._run_pipeline = rec_pipe
    return w, stats


# === 门控五路（验收 7）=========================================================

def test_gate_fresh_mount_skips_rebuild(polling, fast_watch, tmp_path):
    """新鲜跳过（零重建）：rebuild 后重挂 → 纯补齐批次被门控跳过（pipeline 零调用），
    graph.json 不被重写。门控只在 watcher 线程的 flush 内执行——mount 仍无条件入队
    （挂载路径零阻塞，查询路径零新增延迟）。"""
    import graphify.serve_watcher as W
    import rebuild_entry
    root = _mini_proj(tmp_path)
    rebuild_entry.rebuild(root)
    graph_path = root / "graphify-out" / "graph.json"
    mt_before = graph_path.stat().st_mtime_ns
    registry = W.WatcherRegistry(_FakeCache(), debounce=0.1, poll_interval=0.2)
    w, stats = _mount_and_count(registry, root, root / "graphify-out")
    try:
        # 纯补齐批次必须真实发生（mount 仍入队）且被门控跳过（pipeline 零调用）
        assert _wait_for(lambda: stats["flush"] >= 1, timeout=15), "纯补齐批次未 flush"
        time.sleep(0.8)  # 跨多个轮询周期，排除迟到批次
        assert stats["pipeline"] == 0, f"新鲜重挂触发了重建: {stats}"
        assert graph_path.stat().st_mtime_ns == mt_before, "graph.json 被重写"
    finally:
        registry.stop_all()


def test_gate_stale_corpus_rebuilds(polling, fast_watch, tmp_path):
    """真实陈旧执行：改语料不重建 → 重挂 → 门控判定陈旧（mtime）→ 重建出新符号。"""
    import graphify.serve_watcher as W
    import rebuild_entry
    root = _mini_proj(tmp_path)
    rebuild_entry.rebuild(root)
    (root / "a.py").write_text(
        "import b\n\ndef foo():\n    return b.bar()\n\ndef stale_sym():\n    return 1\n",
        encoding="utf-8")
    assert "stale_sym()" not in _labels(root / "graphify-out"), "前置：图未含新语料"
    registry = W.WatcherRegistry(_FakeCache(), debounce=0.1, poll_interval=0.2)
    w, stats = _mount_and_count(registry, root, root / "graphify-out")
    try:
        assert _wait_for(lambda: "stale_sym()" in _labels(root / "graphify-out"), timeout=40), \
            "陈旧重挂未重建出新符号"
        assert stats["pipeline"] >= 1, f"陈旧重挂未执行重建: {stats}"
    finally:
        registry.stop_all()


def test_gate_touch_bump_triggers_rebuild(polling, fast_watch, tmp_path):
    """touch 误报执行（文档化容忍）：仅 mtime 前进无内容变化 → 门控判陈旧 → 重建。

    这是规格接受的安全方向误报——max-mtime 维度无法区分 touch 与真实修改，宁可多
    重建一次也不漏真实修改（误报执行优于静默漏修）。"""
    import graphify.serve_watcher as W
    import rebuild_entry
    root = _mini_proj(tmp_path)
    rebuild_entry.rebuild(root)
    # 强制 mtime 严格前进（+2s，防同秒相等）：内容不变
    future = time.time() + 2
    os.utime(root / "a.py", (future, future))
    registry = W.WatcherRegistry(_FakeCache(), debounce=0.1, poll_interval=0.2)
    w, stats = _mount_and_count(registry, root, root / "graphify-out")
    try:
        assert _wait_for(lambda: stats["pipeline"] >= 1, timeout=40), "touch 未触发重建"
    finally:
        registry.stop_all()


def test_gate_deleted_file_triggers_rebuild(polling, fast_watch, tmp_path):
    """删除触发（count 捕获）：未被监视期间删除文件 → 重挂 → 计数不等 → 重建（无幽灵节点）。

    纯 max-mtime 的删除盲区由此封死——删除文件不动语料 mtime，count 维度是唯一捕获面
    （幽灵节点是本仓反复战斗的回归类）。"""
    import graphify.serve_watcher as W
    import rebuild_entry
    root = _mini_proj(tmp_path)
    rebuild_entry.rebuild(root)
    assert "foo()" in _labels(root / "graphify-out"), "前置：图含 a.py 符号"
    (root / "a.py").unlink()  # 删除 a.py 不重建（count 2 → 1）
    registry = W.WatcherRegistry(_FakeCache(), debounce=0.1, poll_interval=0.2)
    w, stats = _mount_and_count(registry, root, root / "graphify-out")
    try:
        assert _wait_for(lambda: "foo()" not in _labels(root / "graphify-out"), timeout=40), \
            "删除未触发重建（幽灵节点残留）"
        assert stats["pipeline"] >= 1, f"删除重挂未执行重建: {stats}"
    finally:
        registry.stop_all()


def test_gate_scan_timeout_falls_back_unconditional(polling, fast_watch, tmp_path, monkeypatch):
    """扫描超时回退无条件：collect_files 超扫描上界 → 门控放弃判定 → 照常重建。
    （watcher 线程语境下防大仓 flush 停滞的卫生约束。）"""
    import graphify.serve_watcher as W
    import rebuild_entry
    import graphify.extract as EX
    root = _mini_proj(tmp_path)
    rebuild_entry.rebuild(root)
    monkeypatch.setattr(W, "_BACKFILL_SCAN_MAX_S", 0.01)  # 缩短扫描上界到毫秒级
    real_collect = EX.collect_files

    def slow_collect(target, **kw):
        time.sleep(0.5)
        return real_collect(target, **kw)
    monkeypatch.setattr(EX, "collect_files", slow_collect)
    registry = W.WatcherRegistry(_FakeCache(), debounce=0.1, poll_interval=0.2)
    w, stats = _mount_and_count(registry, root, root / "graphify-out")
    try:
        assert _wait_for(lambda: stats["pipeline"] >= 1, timeout=40), "扫描超时未回退无条件重建"
    finally:
        registry.stop_all()


# === I1（final review）：phase != complete 无条件重建（防幽灵节点永久残留）==========

def test_gate_phase_error_forces_rebuild(polling, tmp_path):
    """I1（final review，reviewer 沙盒复现）：phase:error 状态文件（失败重建 shrink-guard
    拒绝 → finally 写 phase:error + 新 source_count，graph.json 停留旧图）→ 门控双匹配
    判 fresh 会幽灵节点永久残留。修：读侧 phase != complete → 无条件重建（单点覆盖
    error+rebuilding 两种载荷）。"""
    import graphify.serve_watcher as W
    import rebuild_entry
    root = _mini_proj(tmp_path)
    rebuild_entry.rebuild(root)
    out = root / "graphify-out"
    state_path = out / ".rebuild-state.json"
    # 构造 phase:error 状态（模拟失败重建后 finally 写的新 count + error 载荷，图停留旧图）
    st = json.loads(state_path.read_text(encoding="utf-8"))
    st["phase"] = "error"
    state_path.write_text(json.dumps(st))
    assert W._should_backfill(root, out) is True, \
        "phase=error 状态应判陈旧（无条件重建，防幽灵节点）"
    # rebuilding 载荷同判（进行中重建 → 状态 count 不可信）
    st["phase"] = "rebuilding"
    state_path.write_text(json.dumps(st))
    assert W._should_backfill(root, out) is True, \
        "phase=rebuilding 状态应判陈旧（无条件重建）"


# === mixed batch 不门控（显式跳过防误用）========================================

def test_gate_mixed_batch_not_gated(polling, tmp_path):
    """mixed batch 不门控：挂载后 debounce 窗内编辑并入同批次 → 直接重建（不吞编辑）。

    补齐与挂载后编辑并入同批次时直接重建（编辑必然使语料变旧、门控也会放行）；显式
    跳过防实现者误将门控套到混合批次吞掉编辑（spec R1 红线 1）。"""
    import graphify.serve_watcher as W
    import rebuild_entry
    root = _mini_proj(tmp_path)
    rebuild_entry.rebuild(root)
    registry = W.WatcherRegistry(_FakeCache(), debounce=0.5, poll_interval=0.2)
    w, stats = _mount_and_count(registry, root, root / "graphify-out")
    try:
        time.sleep(0.05)
        (root / "a.py").write_text(
            "import b\n\ndef foo():\n    return b.bar()\n\ndef merged_sym():\n    return 1\n",
            encoding="utf-8")
        assert _wait_for(lambda: "merged_sym()" in _labels(root / "graphify-out"), timeout=40), \
            "挂载后编辑未落地（mixed batch 被门控吞掉编辑）"
        assert stats["pipeline"] == 1, f"补齐与编辑应并入同批次重建一次: {stats}"
    finally:
        registry.stop_all()


# === 锁 owner：双写路径 source_count 在锁内生效 =================================

def test_watcher_rebuild_updates_source_count(polling, fast_watch, tmp_path):
    """锁 owner：watcher 重建后状态文件 source_count 已更新（_write_state 锁内写入生效）。

    双写路径：rebuild_entry 重建完成 + _run_pipeline 末尾各记一次。锁外调用 _write_state
    会静默丢 count（该函数读锁 pid 判 owner）——watcher 的 pipeline 全程在 _flush_batch
    的锁作用域内，故 source_count 必须可见。挂载补齐与普通编辑批次两条路径都验。"""
    import graphify.serve_watcher as W
    import rebuild_entry
    root = _mini_proj(tmp_path)
    rebuild_entry.rebuild(root)
    state_path = root / "graphify-out" / ".rebuild-state.json"
    assert json.loads(state_path.read_text(encoding="utf-8")).get("source_count") == 2, \
        "前置：rebuild_entry 应写 source_count（双写第 1 点）"
    (root / "c.py").write_text("def cee():\n    return 3\n", encoding="utf-8")
    registry = W.WatcherRegistry(_FakeCache(), debounce=0.1, poll_interval=0.2)
    w, stats = _mount_and_count(registry, root, root / "graphify-out")
    try:
        assert _wait_for(lambda: "cee()" in _labels(root / "graphify-out"), timeout=40), \
            "挂载补齐未重建"
        # graph.json 落盘先于 pipeline 末尾的状态写（写序不变量 E1）——source_count
        # 刷新须轮询等待（非即时断言，防与 _write_source_count 赛跑）
        assert _wait_for(
            lambda: json.loads(state_path.read_text(encoding="utf-8")).get("source_count") == 3,
            timeout=15), "挂载补齐重建后 source_count 未刷新（锁内写入失效）"
        # 普通批次路径：再增一文件 → 编辑批次重建 → count 4
        (root / "d.py").write_text("def dee():\n    return 4\n", encoding="utf-8")
        assert _wait_for(lambda: "dee()" in _labels(root / "graphify-out"), timeout=40), \
            "普通编辑批次未重建"
        assert _wait_for(
            lambda: json.loads(state_path.read_text(encoding="utf-8")).get("source_count") == 4,
            timeout=15), "普通编辑批次重建后 source_count 未刷新"
    finally:
        registry.stop_all()


# === 顺序编辑刷新捕获参照（防 started 停留旧值 → 冗余重建）======================

def test_gate_sequential_edits_refresh_capture_reference(polling, fast_watch, tmp_path):
    """顺序编辑刷新捕获参照：多次普通编辑批次后，状态文件 started 每次刷新 → 下次重挂
    门控判新鲜（零重建）。坏实现（_state_started 仅首批设一次）会让 started 停留旧值，
    重挂门控误判陈旧 → 冗余重建。"""
    import graphify.serve_watcher as W
    import rebuild_entry
    root = _mini_proj(tmp_path)
    rebuild_entry.rebuild(root)
    registry = W.WatcherRegistry(_FakeCache(), debounce=0.1, poll_interval=0.2)
    w, stats = _mount_and_count(registry, root, root / "graphify-out")
    try:
        # 首挂：新鲜 → 门控跳过（pipeline 0）
        assert _wait_for(lambda: stats["flush"] >= 1, timeout=15), "纯补齐批次未 flush"
        assert stats["pipeline"] == 0, f"新鲜首挂触发重建: {stats}"
        # 两次顺序编辑 → 两次普通批次重建（各自刷新 _state_started 捕获参照）
        (root / "a.py").write_text(
            "import b\n\ndef foo():\n    return b.bar()\n\ndef seq1():\n    return 1\n",
            encoding="utf-8")
        assert _wait_for(lambda: "seq1()" in _labels(root / "graphify-out"), timeout=40), \
            "第 1 次编辑未重建"
        time.sleep(0.2)  # 跨过防抖，确保两次编辑分属两个批次
        (root / "a.py").write_text(
            "import b\n\ndef foo():\n    return b.bar()\n\ndef seq1():\n    return 1\n"
            "\ndef seq2():\n    return 2\n",
            encoding="utf-8")
        assert _wait_for(lambda: "seq2()" in _labels(root / "graphify-out"), timeout=40), \
            "第 2 次编辑未重建"
        assert stats["pipeline"] == 2, f"两次编辑应两次重建: {stats}"
    finally:
        registry.stop_all()
    # 重挂（新挂载周期）：图已含 seq2，started 已刷新 → 门控跳过（零重建）
    registry2 = W.WatcherRegistry(_FakeCache(), debounce=0.1, poll_interval=0.2)
    w2, stats2 = _mount_and_count(registry2, root, root / "graphify-out")
    try:
        assert _wait_for(lambda: stats2["flush"] >= 1, timeout=15), "重挂补齐未入队"
        time.sleep(0.5)  # 跨轮询周期，排除迟到批次
        assert stats2["pipeline"] == 0, f"重挂触发冗余重建（started 未刷新）: {stats2}"
    finally:
        registry2.stop_all()


# === 逃生口 GRAPHIFY_BACKFILL=always（US8 回滚旋钮）============================

def test_gate_env_always_escapes_fresh_skip(polling, fast_watch, tmp_path, monkeypatch):
    """逃生口 GRAPHIFY_BACKFILL=always：新鲜项目重挂也无条件补齐（US8 回滚旋钮）。"""
    import graphify.serve_watcher as W
    import rebuild_entry
    monkeypatch.setenv("GRAPHIFY_BACKFILL", "always")
    root = _mini_proj(tmp_path)
    rebuild_entry.rebuild(root)
    registry = W.WatcherRegistry(_FakeCache(), debounce=0.1, poll_interval=0.2)
    w, stats = _mount_and_count(registry, root, root / "graphify-out")
    try:
        assert _wait_for(lambda: stats["pipeline"] >= 1, timeout=40), \
            "GRAPHIFY_BACKFILL=always 未恢复无条件补齐"
    finally:
        registry.stop_all()


# === 适配清单（spec :93）：新鲜夹具测试改逃生口或注入陈旧 =========================

def test_gate_fresh_fixture_adaptation_via_escape_hatch(polling, fast_watch, tmp_path,
                                                       monkeypatch):
    """测试适配清单（spec :93）示例：票 04 新鲜夹具的"挂载即补齐"测试改
    GRAPHIFY_BACKFILL=always 逃生口（或注入真实陈旧）。本测证明逃生口下新鲜夹具的
    挂载补齐语义完整保留（适配后测试语义不丢）。"""
    import graphify.serve_watcher as W
    import rebuild_entry
    monkeypatch.setenv("GRAPHIFY_BACKFILL", "always")
    root = _mini_proj(tmp_path)
    rebuild_entry.rebuild(root)  # 新鲜夹具（无陈旧）
    registry = W.WatcherRegistry(_FakeCache(), debounce=0.1, poll_interval=0.2)
    w, stats = _mount_and_count(registry, root, root / "graphify-out")
    try:
        assert _wait_for(lambda: stats["pipeline"] >= 1, timeout=40), \
            "逃生口下新鲜夹具挂载补齐未触发"
    finally:
        registry.stop_all()


# === I1（reviewer）：未来时间戳粘性参照——钳制 min(max_mtime, now+容差) =============

def test_gate_future_timestamp_edit_not_swallowed(polling, fast_watch, tmp_path):
    """I1（reviewer 实测）：未来时间戳污染参照——touch(+2s) 后重建把 source_max_mtime
    记到未来；随后被监视期间的真实编辑（mtime=now < 未来值）在重挂时被门控判新鲜
    **静默吞掉**（漏修，违反 §R1 漂移安全方向：spec 明文容忍的 touch 误报本应落在
    安全方向=冗余重建，未来参照把它翻成漏修）。修复：快照记录时钳制
    ``min(max_mtime, time.time() + _MTIME_CLAMP_TOLERANCE_S)``——参照恒 ≤ now+1s，
    未来戳语料 max > 参照 → 判陈旧 → 退化安全侧（冗余重建，不吞真实编辑）。"""
    import graphify.serve_watcher as W
    import rebuild_entry
    root = _mini_proj(tmp_path)
    rebuild_entry.rebuild(root)
    # 未来时间戳（touch +2s，秒级 > 1s 容差），重建把参照污染到未来
    future = time.time() + 2
    os.utime(root / "b.py", (future, future))
    rebuild_entry.rebuild(root)
    # 重建后被监视期间的真实编辑（mtime=now < 未来值）
    (root / "a.py").write_text(
        "import b\n\ndef foo():\n    return b.bar()\n\ndef real_sym():\n    return 1\n",
        encoding="utf-8")
    assert "real_sym()" not in _labels(root / "graphify-out"), "前置：图未含真实编辑"
    # 直接断言门控判定（RED→GREEN 判别点，快）：未来参照不得吞真实编辑
    assert W._should_backfill(root, root / "graphify-out") is True, \
        "门控误判新鲜（未来时间戳参照吞真实编辑）"
    # 行为面：重挂后真实编辑被拾取（执行重建）
    registry = W.WatcherRegistry(_FakeCache(), debounce=0.1, poll_interval=0.2)
    w, stats = _mount_and_count(registry, root, root / "graphify-out")
    try:
        assert _wait_for(lambda: "real_sym()" in _labels(root / "graphify-out"), timeout=40), \
            "真实编辑被未来时间戳参照吞掉（门控判新鲜跳过重建）"
        assert stats["pipeline"] >= 1, f"真实编辑未触发重建: {stats}"
    finally:
        registry.stop_all()


def test_gate_clamp_tolerance_boundary(polling, tmp_path):
    """容差边界（controller 裁决）：钳制容差 _MTIME_CLAMP_TOLERANCE_S=1s 内视为时钟源
    偏差被信任（不剪）——文件 mtime now+0.5s → 快照参照保留该值（min(max_mtime, now+1s)
    取前者）；秒级未来戳（touch/NTP，>1s）才被钳制到 ~now+1s。这是"信任容差内偏差、
    钳制秒级未来戳"语义的落点断言。"""
    import graphify.serve_watcher as W
    import rebuild_entry
    # case A: +3s（秒级未来戳）→ 钳到 ~now+1s（恒 ≤ now+1s）
    rootA = _mini_proj(tmp_path / "a")
    futureA = time.time() + 3
    os.utime(rootA / "b.py", (futureA, futureA))
    rebuild_entry.rebuild(rootA)
    stA = json.loads((rootA / "graphify-out" / ".rebuild-state.json").read_text())
    assert stA["source_max_mtime"] <= time.time() + W._MTIME_CLAMP_TOLERANCE_S + 0.05, \
        f"+3s 秒级未来戳未被钳制: {stA['source_max_mtime']}"
    # case B: +0.5s（容差内）→ 保留原值（信任时钟偏差，不剪）
    rootB = _mini_proj(tmp_path / "b")
    futureB = time.time() + 0.5
    os.utime(rootB / "b.py", (futureB, futureB))
    rebuild_entry.rebuild(rootB)
    stB = json.loads((rootB / "graphify-out" / ".rebuild-state.json").read_text())
    assert stB["source_max_mtime"] >= futureB - 1e-6, \
        f"+0.5s 容差内时钟偏差被误剪: {stB['source_max_mtime']}"


# === Major 1（用户终审）：门控扫描先于全局闸——跳过路径零闸占用 ===================

def test_gate_skip_path_zero_gate_acquisition(polling, fast_watch, tmp_path):
    """Major 1（用户终审）：门控扫描先于 gate.acquire()——新鲜重挂跳过路径**零全局闸占用**。

    跨项目场景：A 项目新鲜挂载（最终跳过）不得占用全局信号量（所有 watcher 重建管线互斥），
    B 项目真实编辑可获闸——消除"项目 A 白占闸 2s（_BACKFILL_SCAN_MAX_S），项目 B 真实
    编辑排队"的跨项目串行。本测断言跳过路径的 _flush_batch 从未 acquire 全局闸（零调用）。
    闸前扫描判跳过 → 清 pending（_take_batch 已完成）直接返回，不触碰 gate。

    Major A（评审）：计数闸装在 registry.mount() **之前**——替换 registry._sem，
    _make_watcher 构造 watcher 时即以计数闸为 gate，首个 flush 必经它计数（构造保证，
    非时序运气：装后替换会留下"首次 flush 先于替换"的假绿窗口）。"""
    import graphify.serve_watcher as W
    import rebuild_entry
    root = _mini_proj(tmp_path)
    rebuild_entry.rebuild(root)
    registry = W.WatcherRegistry(_FakeCache(), debounce=0.1, poll_interval=0.2)
    # 计数闸装在 mount 之前：替换 registry._sem（_make_watcher 构造时即用它）
    acquires = {"n": 0}
    real_sem = registry._sem

    class _CountingGate:
        def acquire(self):
            acquires["n"] += 1
            return real_sem.acquire()

        def release(self):
            return real_sem.release()
    registry._sem = _CountingGate()
    w, stats = _mount_and_count(registry, root, root / "graphify-out")
    try:
        assert _wait_for(lambda: stats["flush"] >= 1, timeout=15), "纯补齐批次未 flush"
        time.sleep(0.8)  # 跨多个轮询周期，排除迟到批次
        assert stats["pipeline"] == 0, f"新鲜重挂触发了重建: {stats}"
        assert acquires["n"] == 0, f"跳过路径触碰全局闸 {acquires['n']} 次（应零闸占用）"
    finally:
        registry.stop_all()
