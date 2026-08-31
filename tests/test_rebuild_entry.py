"""rebuild_entry.py 测试 -- 全部 patch 真实存在的模块级符号."""
import sys
from pathlib import Path


def test_sync_runs_before_analysis(monkeypatch, tmp_path):
    """顺序断言：sync 先于 run."""
    import rebuild_entry
    calls = []
    monkeypatch.setattr(rebuild_entry, "run_codegraph_sync", lambda r: (calls.append("sync"), 0)[1])
    monkeypatch.setattr(rebuild_entry, "db_fingerprint", lambda db: (calls.append("fp"), (1, 0))[1])
    monkeypatch.setattr("run_analysis.run", lambda *a, **k: (calls.append("run"), Path(tmp_path))[1])
    # 造一个最小 DB
    import sqlite3
    db = tmp_path / ".codegraph" / "codegraph.db"
    db.parent.mkdir(parents=True)
    c = sqlite3.connect(db); c.execute("CREATE TABLE schema_versions(version INTEGER PRIMARY KEY,applied_at INTEGER,description TEXT)"); c.execute("CREATE TABLE files(path TEXT PRIMARY KEY,content_hash TEXT,language TEXT,size INTEGER,modified_at INTEGER,indexed_at INTEGER,node_count INTEGER,errors TEXT,generated INTEGER)"); c.execute("INSERT INTO schema_versions VALUES(9,0,'x')"); c.commit(); c.close()
    rebuild_entry.rebuild(tmp_path, db_path=db, out_dir=tmp_path/"out", skip_sync=False)
    assert calls.index("sync") < calls.index("run")


def test_rebuild_requeued_on_db_changes_during_rebuild(monkeypatch, tmp_path):
    """db_fingerprint 首查 f0、二查返回不同值 -> run 被调两次（收敛重排）."""
    import rebuild_entry
    import sqlite3
    db = tmp_path / ".codegraph" / "codegraph.db"; db.parent.mkdir(parents=True)
    c = sqlite3.connect(db); c.execute("CREATE TABLE schema_versions(version INTEGER PRIMARY KEY,applied_at INTEGER,description TEXT)"); c.execute("CREATE TABLE files(path TEXT PRIMARY KEY,content_hash TEXT,language TEXT,size INTEGER,modified_at INTEGER,indexed_at INTEGER,node_count INTEGER,errors TEXT,generated INTEGER)"); c.execute("INSERT INTO schema_versions VALUES(9,0,'x')"); c.commit(); c.close()
    monkeypatch.setattr(rebuild_entry, "run_codegraph_sync", lambda r: 0)
    # 循环每轮调 db_fingerprint 两次（f0 快照 + 重建后比对），两轮共 4 次：
    # f0=(1,0) -> 比对(2,0)变 -> 二轮 f0=(2,0) -> 比对(2,0)收敛。
    # （brief 原文只给 3 值，第 4 次 next() 会 StopIteration——修正为 4 值。）
    fingerprints = iter([(1, 0), (2, 0), (2, 0), (2, 0)])
    monkeypatch.setattr(rebuild_entry, "db_fingerprint", lambda d: next(fingerprints))
    run_calls = []
    monkeypatch.setattr("run_analysis.run", lambda *a, **k: run_calls.append(1) or Path(tmp_path))
    rebuild_entry.rebuild(tmp_path, db_path=db, out_dir=tmp_path/"out")
    assert len(run_calls) == 2


def test_sync_failure_retries_then_degrades(monkeypatch, tmp_path):
    """run_codegraph_sync 恒返回 1 -> 重试后 SystemExit(4)，不 crash."""
    import rebuild_entry, pytest
    monkeypatch.setattr(rebuild_entry, "run_codegraph_sync", lambda r: 1)
    db = tmp_path / ".codegraph" / "codegraph.db"; db.parent.mkdir(parents=True)
    import sqlite3; c = sqlite3.connect(db); c.execute("CREATE TABLE schema_versions(version INTEGER PRIMARY KEY,applied_at INTEGER,description TEXT)"); c.execute("INSERT INTO schema_versions VALUES(9,0,'x')"); c.commit(); c.close()
    with pytest.raises(SystemExit) as ex:
        rebuild_entry.rebuild(tmp_path, db_path=db, out_dir=tmp_path/"out")
    assert ex.value.code == 4


def test_lock_held_exits_3(monkeypatch, tmp_path):
    """预建锁目录 -> exit 3（db 检查在锁之后，锁优先触发）。
    F: 用 resolve() 算锁名（与 rebuild() 内部 root.resolve() 一致），防 TEMP 被 junction 重定向时预建锁不命中。"""
    import rebuild_entry, pytest
    lock = rebuild_entry._lock_path(tmp_path.resolve())   # 确定性锁名（路径消毒，非 hash；resolve 对齐 rebuild）
    lock.mkdir(parents=True, exist_ok=True)
    try:
        with pytest.raises(SystemExit) as ex:
            rebuild_entry.rebuild(tmp_path)      # 无 db 但锁先触发
        assert ex.value.code == 3
    finally:
        lock.rmdir()


def test_missing_db_raises(tmp_path):
    """无锁竞争 + 无 .codegraph/ -> FileNotFoundError 且消息含 'codegraph init'（在锁之后检查）。"""
    import rebuild_entry, pytest
    lock = rebuild_entry._lock_path(tmp_path.resolve())   # F: resolve 对齐 rebuild 内部 root.resolve()
    assert not lock.exists()                    # 确保无锁竞争
    with pytest.raises(FileNotFoundError, match="codegraph init"):
        rebuild_entry.rebuild(tmp_path)


def test_rebuild_auto_discovers_default_seed(monkeypatch, tmp_path):
    """C1（最终审查）：semantic_seed 未显式传时，默认发现 <out>/semantic-seed.json 并传给 run().
    背景：watch/_trigger_rebuild 与两个 hook 触发面都不传 --semantic-seed，迁移后首次自动
    重建若不发现种子 -> 语义面丢失 -> 缩量 -> shrink-guard 拒写 -> 触发面永久砖死。"""
    import json as _json
    import sqlite3
    import rebuild_entry
    db = tmp_path / ".codegraph" / "codegraph.db"; db.parent.mkdir(parents=True)
    c = sqlite3.connect(db); c.execute("CREATE TABLE schema_versions(version INTEGER PRIMARY KEY,applied_at INTEGER,description TEXT)"); c.execute("INSERT INTO schema_versions VALUES(9,0,'x')"); c.commit(); c.close()
    seed_path = tmp_path / "graphify-out" / "semantic-seed.json"
    seed_path.parent.mkdir(parents=True)
    seed_path.write_text(_json.dumps({
        "nodes": [{"id": "concept:auto-seed", "label": "自动种子", "_origin": "semantic", "file_type": "concept"}],
        "edges": [], "hyperedges": [],
    }), encoding="utf-8")
    captured = {}
    monkeypatch.setattr(rebuild_entry, "run_codegraph_sync", lambda r: 0)
    monkeypatch.setattr(rebuild_entry, "db_fingerprint", lambda d: (1, 0))
    monkeypatch.setattr("run_analysis.run", lambda *a, **k: captured.update(semantic_seed=k.get("semantic_seed")) or Path(tmp_path))
    rebuild_entry.rebuild(tmp_path, db_path=db)   # 不传 semantic_seed -> 走默认发现
    got = captured.get("semantic_seed")
    assert got is not None, "run() 未收到 seed：默认发现未生效"
    assert Path(got) == seed_path
    # seed 内容含该 semantic 节点
    assert _json.loads(Path(got).read_text(encoding="utf-8"))["nodes"][0]["id"] == "concept:auto-seed"


def test_rebuild_no_seed_file_passes_none(monkeypatch, tmp_path):
    """C1 反向：默认路径无种子文件时 semantic_seed 保持 None（不虚构路径）。"""
    import sqlite3
    import rebuild_entry
    db = tmp_path / ".codegraph" / "codegraph.db"; db.parent.mkdir(parents=True)
    c = sqlite3.connect(db); c.execute("CREATE TABLE schema_versions(version INTEGER PRIMARY KEY,applied_at INTEGER,description TEXT)"); c.execute("INSERT INTO schema_versions VALUES(9,0,'x')"); c.commit(); c.close()
    captured = {}
    monkeypatch.setattr(rebuild_entry, "run_codegraph_sync", lambda r: 0)
    monkeypatch.setattr(rebuild_entry, "db_fingerprint", lambda d: (1, 0))
    monkeypatch.setattr("run_analysis.run", lambda *a, **k: captured.update(semantic_seed=k.get("semantic_seed")) or Path(tmp_path))
    rebuild_entry.rebuild(tmp_path, db_path=db)
    assert captured.get("semantic_seed") is None


def test_stale_lock_taken_over(tmp_path):
    """E: 残留锁（年龄 > _LOCK_STALE_S）被接管清理，而非永久 exit 3。
    模拟进程被强杀后锁残留的场景。"""
    import rebuild_entry, os, time
    lock = rebuild_entry._lock_path(tmp_path.resolve())
    lock.mkdir(parents=True, exist_ok=True)
    (lock / "pid").write_text("99999", encoding="utf-8")
    # 把 pid 文件 mtime 倒拨到 11 分钟前（超 _LOCK_STALE_S=600s）
    old = time.time() - 660
    os.utime(lock / "pid", (old, old))
    # 接管后应进入正常流程（无 db -> FileNotFoundError，而非 exit 3）
    import pytest
    with pytest.raises(FileNotFoundError, match="codegraph init"):
        rebuild_entry.rebuild(tmp_path)


def test_parse_refresh_filters_empty_segments():
    """M1（用户审查）：CLI refresh 解析过滤空段。"a.md," 此前产生
    Path('') -> Path('.')，refresh 指向整个 CWD；与 run_analysis.py CLI 已有的
    空段过滤不一致。None 透传（无 refresh 语义）。"""
    import rebuild_entry
    assert rebuild_entry._parse_refresh("a.md,") == [Path("a.md")]
    assert rebuild_entry._parse_refresh("a.md,,b.md") == [Path("a.md"), Path("b.md")]
    assert rebuild_entry._parse_refresh(None) is None
