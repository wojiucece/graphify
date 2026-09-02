"""A3：单事务快照隔离 + immutable 降级链（只读介质场景用 monkeypatch 模拟）."""
import sqlite3, sys, threading, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import adapter

def _mk_db(tmp_path):
    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY, kind TEXT, name TEXT, "
                 "qualified_name TEXT, file_path TEXT, language TEXT, start_line INT)")
    conn.executemany("INSERT INTO nodes VALUES (?,?,?,?,?,?,?)",
                     [(f"a{i}", "function", f"n{i}", None, "f.py", "python", 1) for i in range(50)])
    conn.commit(); conn.close()
    return db

def test_snapshot_isolation_under_concurrent_write(tmp_path):
    """读取事务期间另一进程写入，读取计数保持快照值（A3 验收第 1 条）."""
    db = _mk_db(tmp_path)
    conn = adapter._open_readonly(db)
    conn.execute("BEGIN")   # 显式开读事务（autocommit 下 SELECT 各自独立）
    n1 = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    writer = sqlite3.connect(db)
    writer.execute("INSERT INTO nodes VALUES ('zz','function','new',NULL,'f.py','python',1)")
    writer.commit(); writer.close()
    n2 = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    conn.close()
    assert n1 == n2 == 50   # 快照隔离：读到的一直是事务开始时的值

def test_readonly_fallback_to_immutable(tmp_path, monkeypatch):
    """mode=ro 打不开（缺 -shm 场景模拟）-> immutable=1 降级 + 日志."""
    db = _mk_db(tmp_path)
    calls = []
    real_connect = sqlite3.connect
    def flaky(uri_style, **kw):
        calls.append(uri_style)
        if "immutable" not in str(uri_style) and len(calls) == 1:
            raise sqlite3.OperationalError("unable to open database file")
        return real_connect(uri_style, **kw)
    monkeypatch.setattr(adapter.sqlite3, "connect", flaky)
    conn = adapter._open_readonly(db, allow_immutable_fallback=True)
    assert conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == 50
    assert any("immutable" in c for c in calls)


def _mk_full_db(tmp_path):
    """load_codegraph 兼容 schema（_mk_db 仅 nodes 表，load_codegraph 尚需
    schema_versions/edges/unresolved_refs，无法直接复用）."""
    db = tmp_path / "full.db"
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE schema_versions (version INTEGER PRIMARY KEY, applied_at INTEGER, description TEXT)")
    conn.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY, kind TEXT, name TEXT, "
                 "qualified_name TEXT, file_path TEXT, language TEXT, start_line INT)")
    conn.execute("CREATE TABLE edges (id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, target TEXT, "
                 "kind TEXT, metadata TEXT, line INTEGER, col INTEGER, provenance TEXT)")
    conn.execute("CREATE TABLE unresolved_refs (id INTEGER PRIMARY KEY AUTOINCREMENT, from_node_id TEXT, "
                 "reference_name TEXT, reference_kind TEXT, line INTEGER, col INTEGER, candidates TEXT, "
                 "file_path TEXT, language TEXT, status TEXT, name_tail TEXT)")
    conn.execute("INSERT INTO schema_versions VALUES (9, 0, 'fake')")
    conn.executemany("INSERT INTO nodes VALUES (?,?,?,?,?,?,?)",
                     [(f"a{i}", "function", f"n{i}", None, "f.py", "python", 1) for i in range(50)])
    conn.commit(); conn.close()
    return db


def test_rebuild_fingerprint_change_logs_without_rerun(tmp_path, monkeypatch, capsys):
    """A3 验收第 2 条：重建期间 DB 变化 -> 记录日志、不再重跑（watch/hook 事件流兜底）."""
    import sqlite3
    import rebuild_entry
    db = tmp_path / ".codegraph" / "codegraph.db"; db.parent.mkdir(parents=True)
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE schema_versions(version INTEGER PRIMARY KEY,applied_at INTEGER,description TEXT)")
    c.execute("CREATE TABLE files(path TEXT PRIMARY KEY,content_hash TEXT,language TEXT,size INTEGER,"
              "modified_at INTEGER,indexed_at INTEGER,node_count INTEGER,errors TEXT,generated INTEGER)")
    c.execute("INSERT INTO schema_versions VALUES(9,0,'x')"); c.commit(); c.close()
    monkeypatch.setattr(rebuild_entry, "run_codegraph_sync", lambda r: 0)
    fingerprints = iter([(1, 0), (2, 0)])   # f0=(1,0)，重建后比对=(2,0)——两轮指纹不同
    monkeypatch.setattr(rebuild_entry, "db_fingerprint", lambda d: next(fingerprints))
    run_calls = []
    monkeypatch.setattr("run_analysis.run", lambda *a, **k: run_calls.append(1) or Path(tmp_path))
    rebuild_entry.rebuild(tmp_path, db_path=db, out_dir=tmp_path / "out")
    assert len(run_calls) == 1               # 只调 run 一次，不再重跑
    err = capsys.readouterr().err
    assert "DB 变化" in err and "不再重跑" in err   # stderr 含变化日志


def test_load_codegraph_opens_read_transaction(tmp_path, monkeypatch):
    """load_codegraph 首语句必须 BEGIN——否则各 SELECT 各自独立快照（autocommit），
    不满足 A3 单事务契约（并发写期间读取不恒为快照值）."""
    db = _mk_full_db(tmp_path)
    real_connect = sqlite3.connect
    records = []

    class _Recording(sqlite3.Connection):
        def execute(self, sql, *a, **kw):
            records.append(str(sql).strip())
            return super().execute(sql, *a, **kw)

    def recording_connect(*a, **kw):
        return real_connect(*a, **kw, factory=_Recording)

    monkeypatch.setattr(adapter.sqlite3, "connect", recording_connect)
    result = adapter.load_codegraph(db)
    assert len(result["nodes"]) == 50
    assert records, "load_codegraph 应至少执行一次 SQL"
    assert records[0].strip().upper() == "BEGIN"   # 单事务：首个语句开启读事务
