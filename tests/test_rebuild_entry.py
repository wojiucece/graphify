"""rebuild_entry.py 测试 -- 新链路编排（Task 09 换源：extract→build→to_json→rebuild_fts→分析）.

codegraph sync/db_fingerprint 已退役——输入源是源码语料（extract 增量 per-file cache），
产物对 = graph.json（事实层）+ .fts-index.db（缓存）+ GRAPH_REPORT.md（分析）。
"""
import json, os, sys, time
from pathlib import Path


def _mini_proj(tmp_path, extra: str = "") -> Path:
    """mini 项目：2 个 Python 文件（可提取语料），返回项目根."""
    (tmp_path / "a.py").write_text(
        "import b\n\ndef foo():\n    return b.bar()\n" + extra, encoding="utf-8")
    (tmp_path / "b.py").write_text("def bar():\n    return 1\n", encoding="utf-8")
    return tmp_path


# === 新链路编排（Task 09 核心）====================================================

def test_rebuild_produces_fact_layer_and_cache(tmp_path):
    """重建入口一键跑通新链路：extract→build→to_json→rebuild_fts→分析。
    产物对齐备：graph.json（事实层）+ .fts-index.db（FTS 缓存）+ GRAPH_REPORT.md."""
    import rebuild_entry
    root = _mini_proj(tmp_path)
    out = Path(rebuild_entry.rebuild(root))
    graph = out / "graph.json"
    fts = out / ".fts-index.db"
    report = out / "GRAPH_REPORT.md"
    assert graph.exists() and fts.exists() and report.exists(), \
        f"产物对缺失: graph={graph.exists()} fts={fts.exists()} report={report.exists()}"
    data = json.loads(graph.read_text(encoding="utf-8"))
    assert "links" in data, "graph.json 必须是 node_link 格式（links 键），serve.py 依赖"
    assert len(data["nodes"]) > 0
    # FTS 缓存可查（bm25 等价检索面生效）
    from fts_cache import is_fresh, open_readonly, fts_search
    assert is_fresh(fts, graph), "FTS 缓存未对事实层保持新鲜"
    conn = open_readonly(fts)
    try:
        rows, hits = fts_search(conn, ["foo"])
        assert hits >= 1, f"FTS 检索 'foo' 应命中 a.foo: {rows}"
    finally:
        conn.close()


def test_pipeline_order_extract_build_tojson_rebuildfts_analysis(monkeypatch, tmp_path):
    """编排顺序：extract → build → to_json → rebuild_fts → run_analysis（换源后的链路）."""
    import graphify.build, graphify.cluster, graphify.export, graphify.extract
    import fts_cache, rebuild_entry, run_analysis
    root = _mini_proj(tmp_path)
    calls = []
    real_extract = graphify.extract.extract
    real_to_json = graphify.export.to_json
    real_rebuild_fts = fts_cache.rebuild_fts
    real_run = run_analysis.run
    real_build = graphify.build.build_from_json

    def fake_extract(paths, **kw):
        calls.append("extract")
        return real_extract(paths, **kw)
    def fake_build(extraction, **kw):
        calls.append("build")
        return real_build(extraction, **kw)
    def fake_to_json(*a, **kw):
        calls.append("to_json")
        return real_to_json(*a, **kw)
    def fake_rebuild_fts(*a, **kw):
        calls.append("rebuild_fts")
        return real_rebuild_fts(*a, **kw)
    def fake_run(*a, **kw):
        calls.append("run_analysis")
        return real_run(*a, **kw)
    monkeypatch.setattr(graphify.extract, "extract", fake_extract)
    monkeypatch.setattr(graphify.build, "build_from_json", fake_build)
    monkeypatch.setattr(graphify.export, "to_json", fake_to_json)
    monkeypatch.setattr(fts_cache, "rebuild_fts", fake_rebuild_fts)
    monkeypatch.setattr(run_analysis, "run", fake_run)
    rebuild_entry.rebuild(root)
    assert calls == ["extract", "build", "to_json", "rebuild_fts", "run_analysis"], \
        f"编排顺序异常: {calls}"


def test_rebuild_empty_project_produces_empty_graph(tmp_path):
    """无代码文件项目：extract 空输入 -> 空事实层，不 crash（旧 '无 db -> FileNotFoundError'
    已随 codegraph 退役消失——新链路无 DB 门控，诚实产出空图）。"""
    import rebuild_entry
    (tmp_path / "README.md").write_text("# docs only\n", encoding="utf-8")
    out = Path(rebuild_entry.rebuild(tmp_path))
    graph = out / "graph.json"
    assert graph.exists()
    data = json.loads(graph.read_text(encoding="utf-8"))
    assert "nodes" in data


def test_rebuild_analysis_reads_written_graph(monkeypatch, tmp_path):
    """分析入口消费 rebuild 落盘的事实层（graph.json 直读，非 DB）——run_analysis.run
    收到的 graph_path 就是 rebuild 写出的 graph.json。"""
    import rebuild_entry
    root = _mini_proj(tmp_path)
    captured = {}
    real_run = None
    import run_analysis
    real_run = run_analysis.run

    def fake_run(graph_path, **kw):
        captured["graph_path"] = Path(graph_path)
        return real_run(graph_path, **kw)
    monkeypatch.setattr(run_analysis, "run", fake_run)
    out = Path(rebuild_entry.rebuild(root))
    assert captured.get("graph_path") == out / "graph.json", \
        f"run() 应读 rebuild 落盘的事实层: {captured.get('graph_path')}"


# === 锁 / stale 接管（Task 09 保留行为）==========================================

def test_lock_held_exits_3(monkeypatch, tmp_path):
    """预建锁目录 -> exit 3（锁优先触发，随后才进新链路）。
    F: 用 resolve() 算锁名（与 rebuild() 内部 root.resolve() 一致），防 TEMP 被 junction 重定向时预建锁不命中。"""
    import rebuild_entry, pytest
    lock = rebuild_entry._lock_path(tmp_path.resolve())
    lock.mkdir(parents=True, exist_ok=True)
    try:
        with pytest.raises(SystemExit) as ex:
            rebuild_entry.rebuild(tmp_path)
        assert ex.value.code == 3
    finally:
        lock.rmdir()


def test_stale_lock_taken_over(tmp_path):
    """E: 残留锁（年龄 > _LOCK_STALE_S）被接管清理，而非永久 exit 3。
    接管后进入新链路正常流程（产出 graph.json——旧 'FileNotFoundError codegraph init'
    已随 DB 门控退役）。"""
    import rebuild_entry, os
    root = _mini_proj(tmp_path)
    lock = rebuild_entry._lock_path(root.resolve())
    lock.mkdir(parents=True, exist_ok=True)
    (lock / "pid").write_text("99999", encoding="utf-8")
    old = time.time() - 660
    os.utime(lock / "pid", (old, old))
    out = Path(rebuild_entry.rebuild(root))
    assert (out / "graph.json").exists()


# === 状态文件 schema v2（Task 09 指纹字段换事实层时间戳）==========================

def test_rebuild_writes_schema2_state(tmp_path):
    """成功路径状态文件 schema=2 + phase=complete；重建载荷 graph_fingerprint =
    graph.json (mtime_ns, size) 指纹（旧 db_fingerprint 的事实层等价物）。"""
    import rebuild_entry
    root = _mini_proj(tmp_path)
    rebuild_entry.rebuild(root)
    state = json.loads(rebuild_entry._state_path(root).read_text(encoding="utf-8"))
    assert state["schema"] == 2, f"状态文件 schema 应升版 v2: {state.get('schema')}"
    assert state["phase"] == "complete"
    assert "graph_fingerprint" not in state  # complete 载荷不携带指纹（与 schema 1 相同）


def test_rebuild_rebuilding_state_has_graph_fingerprint(monkeypatch, tmp_path):
    """rebuilding 载荷含 graph_fingerprint = 重建前事实层 (mtime_ns, size)。"""
    import rebuild_entry
    root = _mini_proj(tmp_path)
    captured = {}
    real_write = rebuild_entry._write_state

    def fake_write(r, lock, payload):
        if payload.get("phase") == "rebuilding":
            captured["payload"] = payload
        return real_write(r, lock, payload)
    monkeypatch.setattr(rebuild_entry, "_write_state", fake_write)
    rebuild_entry.rebuild(root)
    payload = captured.get("payload")
    assert payload is not None, "未捕获 rebuilding 状态"
    assert payload["schema"] == 2
    assert payload["graph_fingerprint"] is None  # 首次重建前无 graph.json
    # 二次重建：已有 graph.json -> 指纹非 None
    captured.clear()
    rebuild_entry.rebuild(root)
    payload = captured.get("payload")
    assert payload["graph_fingerprint"] is not None, "已有事实层时 graph_fingerprint 应为 (mtime_ns, size)"
    assert len(payload["graph_fingerprint"]) == 2


def test_old_schema1_state_file_smoothly_read(tmp_path, capsys):
    """旧状态文件（schema 1）平滑处理：读者只读共字段（git_head/last_duration），
    重建覆盖后新状态为 schema 2。"""
    import subprocess
    import rebuild_entry
    root = _mini_proj(tmp_path)
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
    prev_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
                               text=True).stdout.strip()
    (root / "graphify-out").mkdir()
    rebuild_entry._state_path(root).write_text(json.dumps(
        {"schema": 1, "phase": "complete", "last_duration": 7.5, "git_head": prev_head}),
        encoding="utf-8")
    assert rebuild_entry._read_prev_duration(root) == 7.5
    assert rebuild_entry._read_prev_git_head(root) == prev_head
    rebuild_entry.rebuild(root)
    state = json.loads(rebuild_entry._state_path(root).read_text(encoding="utf-8"))
    assert state["schema"] == 2, "重建后状态文件应升版 schema 2"
    assert state["git_head"] == prev_head


def test_read_prev_duration_non_dict_returns_zero(tmp_path):
    """终审 Imp-1 连带：状态文件非 dict（[]）→ _read_prev_duration 返回 0.0 不崩."""
    import rebuild_entry
    state = tmp_path / "graphify-out" / ".rebuild-state.json"
    state.parent.mkdir()
    state.write_text("[]", encoding="utf-8")
    assert rebuild_entry._read_prev_duration(tmp_path) == 0.0


def test_rebuild_state_tracks_git_head(tmp_path):
    """git 仓库重建 -> complete 状态含 git_head == rev-parse HEAD（基线锚点保留）。"""
    import subprocess
    import rebuild_entry
    root = _mini_proj(tmp_path)
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
    rebuild_entry.rebuild(root)
    data = json.loads(rebuild_entry._state_path(root).read_text(encoding="utf-8"))
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
                          text=True).stdout.strip()
    assert data["git_head"] == head


# === 语义种子合并（从 run_analysis 迁入 rebuild_entry 的 build 步骤）===============

def test_rebuild_merges_seed_and_attaches_hyperedges(tmp_path):
    """seed 合并 + hyperedges 挂回：语义节点进图 + 顶层 hyperedges 存在."""
    import rebuild_entry
    root = _mini_proj(tmp_path)
    seed = {
        "nodes": [{"id": "concept:auth", "label": "认证", "_origin": "semantic", "file_type": "concept"}],
        "edges": [{"source": "concept:auth", "target": "file:a.py", "relation": "documents"}],
        "hyperedges": [{"id": "auth_cluster", "label": "认证簇", "nodes": ["concept:auth"]}],
    }
    seed_path = tmp_path / "seed.json"
    seed_path.write_text(json.dumps(seed), encoding="utf-8")
    out = Path(rebuild_entry.rebuild(root, semantic_seed=seed_path))
    data = json.loads((out / "graph.json").read_text(encoding="utf-8"))
    ids = {n["id"] for n in data["nodes"]}
    assert "concept:auth" in ids
    hes = data.get("hyperedges") or data.get("graph", {}).get("hyperedges") or []
    assert any(h["id"] == "auth_cluster" for h in hes), "hyperedges 未挂回"


def test_rebuild_auto_discovers_default_seed(tmp_path):
    """C1（最终审查）：semantic_seed 未显式传时，默认发现 <out>/semantic-seed.json 并合入."""
    import rebuild_entry
    root = _mini_proj(tmp_path)
    seed_path = tmp_path / "graphify-out" / "semantic-seed.json"
    seed_path.parent.mkdir(parents=True)
    seed_path.write_text(json.dumps({
        "nodes": [{"id": "concept:auto-seed", "label": "自动种子", "_origin": "semantic", "file_type": "concept"}],
        "edges": [], "hyperedges": [],
    }), encoding="utf-8")
    out = Path(rebuild_entry.rebuild(root))
    data = json.loads((out / "graph.json").read_text(encoding="utf-8"))
    ids = {n["id"] for n in data["nodes"]}
    assert "concept:auto-seed" in ids, "默认种子未自动合入"


def test_rebuild_no_seed_file_produces_ast_only(tmp_path):
    """默认路径无种子文件 -> 不虚构 seed，图仅 AST 语义面."""
    import rebuild_entry
    root = _mini_proj(tmp_path)
    out = Path(rebuild_entry.rebuild(root))
    data = json.loads((out / "graph.json").read_text(encoding="utf-8"))
    assert not any(n.get("_origin") == "semantic" for n in data["nodes"])


def test_rebuild_warns_on_symbolic_anchor_violations(tmp_path, capsys):
    """I1/B2：seed 含符号级锚点（function:xxx）-> stderr 告警但不阻断."""
    import rebuild_entry
    root = _mini_proj(tmp_path)
    seed = {
        "nodes": [{"id": "concept:auth", "label": "认证", "_origin": "semantic", "file_type": "concept"}],
        "edges": [{"source": "concept:auth", "target": "function:abc123", "relation": "documents"}],
        "hyperedges": [],
    }
    seed_path = tmp_path / "seed.json"
    seed_path.write_text(json.dumps(seed), encoding="utf-8")
    out = Path(rebuild_entry.rebuild(root, semantic_seed=seed_path))
    captured = capsys.readouterr()
    assert "语义锚定违规" in captured.err, f"stderr 未含锚定违规告警: {captured.err!r}"
    assert "function:abc123" in captured.err
    assert (out / "graph.json").exists()


def test_rebuild_warns_on_missing_explicit_seed(tmp_path, capsys):
    """显式 --semantic-seed 传不存在路径 -> stderr 警告（fail-loud 保留），run 不抛异常."""
    import rebuild_entry
    root = _mini_proj(tmp_path)
    missing = tmp_path / "nonexistent.json"
    out = Path(rebuild_entry.rebuild(root, semantic_seed=missing))
    assert (out / "graph.json").exists()
    captured = capsys.readouterr()
    assert "semantic-seed" in captured.err or str(missing) in captured.err


def test_refresh_persists_seed_and_prevents_shrink_guard_brick(tmp_path, monkeypatch):
    """砖死回归（用户实测复现，两轮沙箱模拟）：refresh 产物落盘 <out>/semantic-seed.json
    （与 C1 默认发现路径一致），下一轮无 refresh 的 rebuild 自动拾取，链路闭合不再砖死。"""
    import rebuild_entry
    root = _mini_proj(tmp_path)
    md = root / "notes.md"
    md.write_text("# 架构决策\n\n采用 SQLite 存储元数据。\n\n## 原因\n\n单文件部署最简单。\n",
                  encoding="utf-8")
    out = Path(rebuild_entry.rebuild(root, semantic_refresh=[md]))
    seed_path = out / "semantic-seed.json"
    assert seed_path.exists(), "refresh 产物未落盘 semantic-seed.json"
    seed = json.loads(seed_path.read_text(encoding="utf-8"))
    assert any(n.get("_origin") == "semantic" for n in seed.get("nodes", [])), \
        "seed 无 _origin=semantic 节点"
    sem_ids1 = {n["id"] for n in json.loads((out / "graph.json").read_text(encoding="utf-8"))
                .get("nodes", []) if n.get("_origin") == "semantic"}
    assert sem_ids1, "第一轮 graph.json 无 semantic 节点"
    # 第二轮：无 refresh（模拟 .py 编辑 / SessionEnd hook 触发面）。
    out2 = Path(rebuild_entry.rebuild(root))
    graph2 = json.loads((out2 / "graph.json").read_text(encoding="utf-8"))
    sem_ids2 = {n["id"] for n in graph2.get("nodes", []) if n.get("_origin") == "semantic"}
    assert sem_ids1.issubset(sem_ids2), f"第二轮丢失 semantic 节点: {sem_ids1 - sem_ids2}"


def test_refresh_upsert_no_duplicate_growth(tmp_path):
    """upsert 语义（按 source_file 替换，防重复膨胀）：同一 .md 连续两次 refresh，
    seed 中该文件的节点数不翻倍。"""
    import rebuild_entry
    root = _mini_proj(tmp_path)
    md = root / "notes.md"
    md.write_text("# 架构决策\n\n采用 SQLite 存储元数据。\n\n## 原因\n\n单文件部署最简单。\n",
                  encoding="utf-8")
    out = Path(rebuild_entry.rebuild(root, semantic_refresh=[md]))
    seed1 = json.loads((out / "semantic-seed.json").read_text(encoding="utf-8"))
    count1 = len([n for n in seed1.get("nodes", []) if n.get("source_file") == "notes.md"])
    assert count1 > 0, "第一次 refresh 后 seed 无该文件节点"
    rebuild_entry.rebuild(root, semantic_refresh=[md])
    seed2 = json.loads((out / "semantic-seed.json").read_text(encoding="utf-8"))
    count2 = len([n for n in seed2.get("nodes", []) if n.get("source_file") == "notes.md"])
    assert count2 == count1, f"第二次 refresh 后 seed 节点数膨胀: {count1} -> {count2}"


def test_rebuild_shrink_guard_blocks_silent_overwrite(tmp_path):
    """B4/审查 fix：to_json force=False 恢复 #479 shrink-guard——现有图节点数 >= 新图时
    RuntimeError 且 graph.json 保持旧内容不被覆盖。"""
    import rebuild_entry, pytest
    root = _mini_proj(tmp_path)
    out = Path(rebuild_entry.rebuild(root))
    graph = out / "graph.json"
    first_n = len(json.loads(graph.read_text(encoding="utf-8"))["nodes"])
    assert first_n > 0
    # 模拟缩量：把现有 graph.json 换成节点更多的伪造 node_link JSON。
    fake = {
        "nodes": [{"id": f"fake:{i}", "label": f"fake{i}"} for i in range(first_n + 50)],
        "links": [],
    }
    graph.write_text(json.dumps(fake), encoding="utf-8")
    with pytest.raises(RuntimeError, match="shrink-guard"):
        rebuild_entry.rebuild(root)
    assert json.loads(graph.read_text(encoding="utf-8")) == fake  # 未被覆盖


# === A3 收敛 / 变更摘要 / refresh 解析 ===========================================

def test_rebuild_concurrent_graph_change_logs_without_rerun(monkeypatch, tmp_path, capsys):
    """A3（Task 9 换源）：重建期间事实层被并发推进（分析窗口期 graph.json 指纹变化）
    -> stderr 记录日志、不再重跑（watch/hook 事件流兜底）。"""
    import fts_cache, rebuild_entry
    root = _mini_proj(tmp_path)
    calls = {"fp": 0}
    def fake_fp(graph_path):
        calls["fp"] += 1
        return (calls["fp"], 0)   # 每次调用指纹递增（模拟并发推进）
    monkeypatch.setattr(fts_cache, "fingerprint", fake_fp)
    rebuild_entry.rebuild(root)
    err = capsys.readouterr().err
    assert "并发推进" in err and "不再重跑" in err


def test_parse_refresh_filters_empty_segments():
    """M1（用户审查）：CLI refresh 解析过滤空段。"a.md," 不产生 Path('')."""
    import rebuild_entry
    assert rebuild_entry._parse_refresh("a.md,") == [Path("a.md")]
    assert rebuild_entry._parse_refresh("a.md,,b.md") == [Path("a.md"), Path("b.md")]
    assert rebuild_entry._parse_refresh(None) is None


def test_log_changed_summary_orphan_head_silent(tmp_path, capsys):
    """M2（用户补充审核）：孤儿 prev_head（传入但 git diff 失败）-> 静默返回——
    stderr 无"变更摘要"且不抛异常。"""
    import subprocess
    import rebuild_entry
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)
    rebuild_entry._log_changed_summary(tmp_path, "0" * 40)   # 假孤儿 head（不存在）
    err = capsys.readouterr().err
    assert "变更摘要" not in err


def test_rebuild_logs_changed_summary(monkeypatch, tmp_path, capsys):
    """rebuild 成功后 stderr 附加一行变更摘要（git diff --name-only <prev>..HEAD）."""
    import subprocess
    import rebuild_entry
    root = _mini_proj(tmp_path)
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
    prev_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
                               text=True).stdout.strip()
    (root / "graphify-out").mkdir()
    rebuild_entry._state_path(root).write_text(json.dumps(
        {"schema": 1, "phase": "complete", "git_head": prev_head}), encoding="utf-8")
    (root / "c.py").write_text("def c():\n    pass\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "second"], cwd=root, check=True, capture_output=True)
    rebuild_entry.rebuild(root)
    err = capsys.readouterr().err
    assert "变更摘要" in err and "c.py" in err
