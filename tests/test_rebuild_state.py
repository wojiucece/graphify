"""A1a 状态文件：写权单一化 + finally 覆盖 + schema 版本化."""
import json, os, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import pytest
from rebuild_entry import _state_path, _write_state, _lock_path

@pytest.fixture
def proj(tmp_path):
    (tmp_path / "graphify-out").mkdir()
    (tmp_path / ".codegraph").mkdir()
    return tmp_path

def test_state_path_is_under_graphify_out(proj):
    assert _state_path(proj) == proj / "graphify-out" / ".rebuild-state.json"

def test_write_state_requires_lock_owner(proj):
    lock = _lock_path(proj); lock.mkdir()
    (lock / "pid").write_text(str(os.getpid()))
    _write_state(proj, lock, {"schema": 2, "phase": "rebuilding", "started": time.time()})
    data = json.loads(_state_path(proj).read_text(encoding="utf-8"))
    assert data["phase"] == "rebuilding" and data["schema"] == 2

def test_write_state_refuses_non_owner(proj):
    lock = _lock_path(proj); lock.mkdir()
    (lock / "pid").write_text("999999999")   # 非自身 pid
    before = _state_path(proj).exists()
    _write_state(proj, lock, {"schema": 2, "phase": "rebuilding", "started": time.time()})
    assert _state_path(proj).exists() == before  # 拒写，文件不出现

def test_rebuild_finally_updates_state_even_on_error(proj, monkeypatch):
    """异常路径不留 rebuilding 残留（A1 验收第 4 条）."""
    import rebuild_entry as re_mod
    lock = _lock_path(proj); lock.mkdir()
    (lock / "pid").write_text(str(os.getpid()))
    # 直接测 finally 语义：模拟 run 抛异常后状态被覆盖
    (proj / "graphify-out" / ".rebuild-state.json").write_text(
        json.dumps({"schema": 1, "phase": "rebuilding", "started": time.time()}), encoding="utf-8")
    re_mod._finish_state(proj, lock, started=time.time() - 5, error=True)
    data = json.loads(_state_path(proj).read_text(encoding="utf-8"))
    assert data["phase"] == "error" and "last_duration" in data

def test_read_prev_duration_inherits_from_complete(proj):
    """N2：rebuilding 载荷继承上轮 complete 的 last_duration（否则时效逃生 2x 项恒 0）."""
    import rebuild_entry as re_mod
    (proj / "graphify-out" / ".rebuild-state.json").write_text(
        json.dumps({"schema": 1, "phase": "complete", "last_duration": 42.5}), encoding="utf-8")
    assert re_mod._read_prev_duration(proj) == 42.5

def test_read_prev_duration_missing_or_corrupt_is_zero(proj):
    import rebuild_entry as re_mod
    assert re_mod._read_prev_duration(proj) == 0.0          # 文件不存在
    (proj / "graphify-out" / ".rebuild-state.json").write_text("not json", encoding="utf-8")
    assert re_mod._read_prev_duration(proj) == 0.0          # 损坏不炸
