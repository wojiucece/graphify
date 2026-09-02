"""C3：git diff -> 文件集 -> 符号集；孤儿 hash 回退 graph_diff；非 git 仓库 no-op；git_head 缺失回退."""
import json, subprocess, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # import graphify.serve（注册测试）
import pytest
from git_symbols import changed_symbols

@pytest.fixture
def git_proj(tmp_path):
    def _git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "a.py").write_text("def kept():\n    pass\n", encoding="utf-8")
    _git("init"); _git("config", "user.email", "t@t"); _git("config", "user.name", "t")
    _git("add", "."); _git("commit", "-m", "init")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True,
                          text=True).stdout.strip()
    (tmp_path / "b.py").write_text("def added():\n    pass\n", encoding="utf-8")
    _git("add", "."); _git("commit", "-m", "second")
    return tmp_path, head

def test_changed_symbols_matches_git_diff(git_proj):
    proj, head = git_proj
    r = changed_symbols(proj, from_head=head)
    assert "b.py" in " ".join(r["files"]) and r["basis"] == "git_head"

def test_non_git_repo_noop(tmp_path):
    r = changed_symbols(tmp_path)
    assert r["git_available"] is False and r["symbol_ids"] == []

def test_missing_git_head_falls_back(tmp_path):
    """G3：状态文件无 git_head（git 不可用/首次运行）-> 不报错，回退 graph_diff 基线."""
    out = tmp_path / "graphify-out"; out.mkdir()
    (out / ".rebuild-state.json").write_text(
        json.dumps({"schema": 1, "phase": "complete"}), encoding="utf-8")   # 无 git_head 字段
    r = changed_symbols(tmp_path)
    assert r["git_available"] is False and r["basis"] == "graph_diff"   # 与孤儿 hash 同出口

def test_missing_git_head_in_git_repo_falls_back(git_proj):
    """G3 精确形态：git 仓库 + 状态文件无 git_head -> from_head=None -> 回退 graph_diff
    （git 可用但基线未锚定，git_available=True 如实反映；basis 标记降级）."""
    proj, head = git_proj
    r = changed_symbols(proj)   # from_head=None（serve 侧从状态文件读不到 git_head 时）
    assert r["git_available"] is True and r["basis"] == "graph_diff"
    assert r["symbol_ids"] == []

def test_orphan_hash_falls_back_to_graph_diff(git_proj):
    """孤儿 hash（amend/rebase 后旧 commit 不可达）-> git diff 报错 -> 捕获回退 graph_diff."""
    proj, head = git_proj
    r = changed_symbols(proj, from_head="0" * 40)   # 不存在的 commit hash
    assert r["git_available"] is True and r["basis"] == "graph_diff"
    assert r["symbol_ids"] == []

def test_worktree_dirty_diff_included(git_proj):
    """git diff --name-only <from_head>..HEAD ∪ 工作区 diff（--name-only HEAD）.
    git diff 只报已跟踪文件的未提交修改（untracked 新文件不属 diff 语义，不在本期范围）."""
    proj, head = git_proj
    (proj / "a.py").write_text("def kept():\n    pass\n    x = 2\n", encoding="utf-8")
    r = changed_symbols(proj, from_head=head)
    assert "a.py" in r["files"] and r["basis"] == "git_head"

def test_git_head_path_queries_db_symbols(git_proj):
    """文件集 -> DB 直查 nodes.file_path IN 文件集 -> 命中变更文件内节点（不含未变文件）."""
    import sqlite3
    proj, head = git_proj
    db = proj / ".codegraph" / "codegraph.db"; db.parent.mkdir()
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE nodes(id TEXT PRIMARY KEY, kind TEXT, name TEXT, "
              "qualified_name TEXT, file_path TEXT)")
    c.execute("INSERT INTO nodes VALUES('id:b', 'function', 'added', 'b.added', 'b.py')")
    c.execute("INSERT INTO nodes VALUES('id:a', 'function', 'kept', 'a.kept', 'a.py')")
    c.commit(); c.close()
    r = changed_symbols(proj, from_head=head)
    assert "b.py" in r["files"]
    assert "id:b" in r["symbol_ids"] and "id:a" not in r["symbol_ids"]
    assert any(s["id"] == "id:b" and s["file"] == "b.py" for s in r["symbols"])

def test_db_missing_returns_empty_symbols(git_proj):
    """DB 缺失（无 .codegraph/codegraph.db）-> 诚实空符号集，不崩出口."""
    proj, head = git_proj
    r = changed_symbols(proj, from_head=head)
    assert r["basis"] == "git_head" and r["symbol_ids"] == []

# --- serve 注册（N1 契约）---
def test_serve_registers_changed_symbols():
    """serve 注册：登记 _SEARCH_TOOLS + N1 信封装配（检索型三元组，C 信封纪律不走 override）."""
    from graphify.serve import _SEARCH_TOOLS, _apply_envelope
    assert "get_changed_symbols" in _SEARCH_TOOLS
    out = _apply_envelope("get_changed_symbols", ("Changed symbols...", True, 3), freshness="fresh")
    meta = json.loads(out.rstrip("\n").split("\n")[-1].removeprefix("_meta: "))
    assert meta["verdict"] == "ok"
    # 无变更信息（非 git / 回退）-> found=False -> absent（"没有变更信息"≠ok）
    out_absent = _apply_envelope("get_changed_symbols", ("Changed symbols...", False, 0),
                                 freshness="fresh")
    meta2 = json.loads(out_absent.rstrip("\n").split("\n")[-1].removeprefix("_meta: "))
    assert meta2["verdict"] == "absent"

def test_clean_repo_headers_equal_baseline(git_proj):
    """Imp-1：基线==HEAD 且工作区干净（rebuild 刚完成后最常见调用）-> basis=git_head 空
    文件集，正文 No changed files，不得谎报 "baseline unavailable"（基线明明锚定成功）。
    对照：basis=graph_diff 空集仍报 unavailable（基线确实未锚定）."""
    from graphify.serve import _format_changed_symbols
    proj, _head = git_proj
    cur = subprocess.run(["git", "rev-parse", "HEAD"], cwd=proj, capture_output=True,
                         text=True).stdout.strip()
    r = changed_symbols(proj, from_head=cur)   # HEAD==基线 -> git diff 空 + 工作区干净
    assert r["basis"] == "git_head" and r["files"] == []
    out = _format_changed_symbols(r, cur)
    assert "No changed files" in out and "unavailable" not in out
    r2 = {"files": [], "symbol_ids": [], "symbols": [], "basis": "graph_diff",
          "git_available": False}
    out2 = _format_changed_symbols(r2, None)
    assert "unavailable" in out2

def test_query_symbols_batches_large_file_set(git_proj, capsys):
    """Minor-2：501 个文件 -> 2 批 IN 查询全部命中（防 SQLite 变量上限临界静默部分失败）
    + 超过一批时 stderr 告警（防静默）."""
    import sqlite3
    from git_symbols import _query_symbols
    proj, _head = git_proj
    db = proj / ".codegraph" / "codegraph.db"; db.parent.mkdir()
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE nodes(id TEXT PRIMARY KEY, kind TEXT, name TEXT, "
              "qualified_name TEXT, file_path TEXT)")
    for i in range(501):
        c.execute("INSERT INTO nodes VALUES(?, 'function', ?, ?, ?)",
                  (f"id:{i}", f"f{i}", f"f{i}.f{i}", f"f{i:03d}.py"))
    c.commit(); c.close()
    files = [f"f{i:03d}.py" for i in range(501)]
    syms = _query_symbols(proj, files)
    assert len(syms) == 501
    assert {s["id"] for s in syms} == {f"id:{i}" for i in range(501)}
    err = capsys.readouterr().err
    assert "批 IN 查询" in err and "2 批" in err   # 超过一批时 stderr 告警（防静默）

def test_read_prev_git_head_non_dict(tmp_path):
    """Minor-3：状态文件合法 JSON 但非 dict（如 []）-> 返回 None 不抛——AttributeError
    防护（调用点在 rebuild() 的 try/finally 外，抛了会遗留 stale 锁）."""
    import rebuild_entry
    (tmp_path / "graphify-out").mkdir()
    rebuild_entry._state_path(tmp_path).write_text("[]", encoding="utf-8")
    assert rebuild_entry._read_prev_git_head(tmp_path) is None
    rebuild_entry._state_path(tmp_path).write_text('"str"', encoding="utf-8")
    assert rebuild_entry._read_prev_git_head(tmp_path) is None

# --- rebuild_entry git_head 记账（Task 10 Step 4）---
def _git_repo(tmp_path):
    """单 commit 的 tmp git 仓库（rebuild_entry git_head 记账测试用）."""
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)
    return tmp_path

def test_finish_state_writes_git_head(tmp_path):
    """成功路径 _finish_state 载荷加 git_head == rev-parse HEAD."""
    import os, time
    import rebuild_entry
    proj = _git_repo(tmp_path)
    (proj / "graphify-out").mkdir()   # _state_path 不建父目录，须先存在（既有测试同款）
    lock = rebuild_entry._lock_path(proj.resolve()); lock.mkdir()
    (lock / "pid").write_text(str(os.getpid()))
    rebuild_entry._finish_state(proj, lock, started=time.time())
    data = json.loads(rebuild_entry._state_path(proj).read_text(encoding="utf-8"))
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=proj, capture_output=True,
                          text=True).stdout.strip()
    assert data["git_head"] == head

def test_finish_state_omits_git_head_non_git(tmp_path):
    """git 不可用/非 git 仓库 -> 省略 git_head 字段（schema 只增不改，可缺省）."""
    import os, time
    import rebuild_entry
    (tmp_path / "graphify-out").mkdir()
    lock = rebuild_entry._lock_path(tmp_path.resolve()); lock.mkdir()
    (lock / "pid").write_text(str(os.getpid()))
    rebuild_entry._finish_state(tmp_path, lock, started=time.time())
    data = json.loads(rebuild_entry._state_path(tmp_path).read_text(encoding="utf-8"))
    assert "git_head" not in data

def test_read_prev_git_head(tmp_path):
    """rebuild 覆盖前缓存上一轮 git_head（变更摘要锚点）；缺失/损坏 -> None."""
    import rebuild_entry
    (tmp_path / "graphify-out").mkdir()
    assert rebuild_entry._read_prev_git_head(tmp_path) is None          # 文件不存在
    rebuild_entry._state_path(tmp_path).write_text(
        json.dumps({"schema": 1, "phase": "complete"}), encoding="utf-8")
    assert rebuild_entry._read_prev_git_head(tmp_path) is None          # 无 git_head 字段
    rebuild_entry._state_path(tmp_path).write_text(
        json.dumps({"schema": 1, "phase": "complete", "git_head": "abc1234"}), encoding="utf-8")
    assert rebuild_entry._read_prev_git_head(tmp_path) == "abc1234"
    rebuild_entry._state_path(tmp_path).write_text("not json", encoding="utf-8")
    assert rebuild_entry._read_prev_git_head(tmp_path) is None          # 损坏不炸

def test_rebuild_logs_changed_summary(monkeypatch, tmp_path, capsys):
    """rebuild 成功后 stderr 附加一行变更摘要（git diff --name-only <prev>..HEAD）."""
    import sqlite3
    import rebuild_entry
    proj = _git_repo(tmp_path)
    prev_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=proj, capture_output=True,
                               text=True).stdout.strip()
    (proj / "graphify-out").mkdir()
    rebuild_entry._state_path(proj).write_text(
        json.dumps({"schema": 1, "phase": "complete", "git_head": prev_head}), encoding="utf-8")
    # 第二个 commit：新增 b.py
    (proj / "b.py").write_text("def b():\n    pass\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=proj, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "second"], cwd=proj, check=True, capture_output=True)
    db = proj / ".codegraph" / "codegraph.db"; db.parent.mkdir()
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE schema_versions(version INTEGER PRIMARY KEY,applied_at INTEGER,description TEXT)")
    c.execute("INSERT INTO schema_versions VALUES(9,0,'x')"); c.commit(); c.close()
    monkeypatch.setattr(rebuild_entry, "run_codegraph_sync", lambda r: 0)
    monkeypatch.setattr(rebuild_entry, "db_fingerprint", lambda d: (1, 0))
    monkeypatch.setattr("run_analysis.run", lambda *a, **k: Path(tmp_path))
    rebuild_entry.rebuild(proj, db_path=db, out_dir=proj / "graphify-out")
    err = capsys.readouterr().err
    assert "变更摘要" in err and "b.py" in err
