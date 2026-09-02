"""C4：hotspots = churn（git log 文件 commit 频次）× 度数代理（DB edges source 端 GROUP BY）top-N.

覆盖：churn 计数（含中文文件名 quotepath）、churn×degree 交叉积排序（非 churn 单轴）、
零分剔除与 scanned 口径、top_n 钳制、非 git no-op、DB 缺失度数轴缺失、dangling 边剔除、
serve 注册（_SEARCH_TOOLS + N1 信封，C 信封纪律不走 override）、正文 declared 代理声明、
空结果四分支诚实文案。
"""
import json, subprocess, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # import graphify.serve（注册测试）
import pytest
from git_symbols import hotspots, _churn, _degree


def _git_run(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _mk_git_repo(tmp_path):
    """tmp git 仓库（生产形态：真实 git 历史）：a.py 2 次、b.py 1 次、c.py 2 次 commit.

    churn: a.py=2, b.py=1, c.py=2——a 与 c churn 相同，由度数轴区分排序（交叉积语义）。
    .git/info/exclude 加 .codegraph/（生产仓 .gitignore:17 效果同源——否则 _mk_db 的
    DB 文件会被 git add . 提交进历史污染 churn；exclude 不进 git 历史，不产生额外
    churn 文件）。"""
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git_run(tmp_path, "init"); _git_run(tmp_path, "config", "user.email", "t@t")
    _git_run(tmp_path, "config", "user.name", "t")
    (tmp_path / ".git" / "info").mkdir(exist_ok=True)   # git init 已建 info/（含样例）
    (tmp_path / ".git" / "info" / "exclude").write_text(".codegraph/\n", encoding="utf-8")
    _git_run(tmp_path, "add", "."); _git_run(tmp_path, "commit", "-m", "c1")   # a.py
    (tmp_path / "b.py").write_text("y = 1\n", encoding="utf-8")
    _git_run(tmp_path, "add", "."); _git_run(tmp_path, "commit", "-m", "c2")   # b.py
    (tmp_path / "a.py").write_text("x = 2\n", encoding="utf-8")
    _git_run(tmp_path, "add", "."); _git_run(tmp_path, "commit", "-m", "c3")   # a.py
    (tmp_path / "c.py").write_text("z = 1\n", encoding="utf-8")
    _git_run(tmp_path, "add", "."); _git_run(tmp_path, "commit", "-m", "c4")   # c.py
    (tmp_path / "c.py").write_text("z = 2\n", encoding="utf-8")
    _git_run(tmp_path, "add", "."); _git_run(tmp_path, "commit", "-m", "c5")   # c.py
    return tmp_path


def _mk_db(root, degrees, dangling=0):
    """最小 codegraph DB：nodes/edges 引用列与生产 schema 同形（test_git_symbols 同口径）.

    hotspots 的度数查询只引用 nodes(id,file_path) + edges(source)——fixture 按引用列
    同形创建（禁手写理想化：列名与真实 DB 对齐）。dangling>0 时追加 source 端点缺失的边。"""
    import sqlite3
    db = root / ".codegraph" / "codegraph.db"
    db.parent.mkdir(exist_ok=True)
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE nodes(id TEXT PRIMARY KEY, kind TEXT, name TEXT, "
              "qualified_name TEXT, file_path TEXT)")
    c.execute("CREATE TABLE edges(id TEXT PRIMARY KEY, source TEXT, target TEXT, "
              "kind TEXT, provenance TEXT)")
    n = 0
    for file, deg in degrees.items():
        nid = f"id:{file}"
        c.execute("INSERT INTO nodes VALUES(?,?,?,?,?)",
                  (nid, "function", "f", f"f.{file}", file))
        for _ in range(deg):
            n += 1
            c.execute("INSERT INTO edges VALUES(?,?,?,?,?)",
                      (f"e{n}", nid, f"t{n}", "calls", "raw"))
    for i in range(dangling):
        n += 1
        c.execute("INSERT INTO edges VALUES(?,?,?,?,?)",
                  (f"e{n}", f"ghost{i}", f"t{n}", "calls", "raw"))
    c.commit(); c.close()


# --- churn 轴 ---
def test_churn_counts_commits_per_file(tmp_path):
    proj = _mk_git_repo(tmp_path)
    churn = _churn(proj)
    assert churn["a.py"] == 2 and churn["b.py"] == 1 and churn["c.py"] == 2


def test_churn_non_ascii_filename(tmp_path):
    """中文文件名：core.quotepath=off 下 git 原样输出 UTF-8 路径（默认 quotepath 会
    八进制转义+引号包裹，与 DB nodes.file_path 坐标 join 断裂——用户仓实际含中文路径）."""
    (tmp_path / "说明.md").write_text("# 文档\n", encoding="utf-8")
    _git_run(tmp_path, "init"); _git_run(tmp_path, "config", "user.email", "t@t")
    _git_run(tmp_path, "config", "user.name", "t")
    _git_run(tmp_path, "add", "."); _git_run(tmp_path, "commit", "-m", "c1")
    churn = _churn(tmp_path)
    assert churn.get("说明.md") == 1


def test_churn_git_failure_returns_empty(tmp_path):
    """git log 失败（空仓库无提交）-> 空 churn，不炸（调用方 scanned=0 -> 无提交史分支）."""
    _git_run(tmp_path, "init"); _git_run(tmp_path, "config", "user.email", "t@t")
    _git_run(tmp_path, "config", "user.name", "t")
    assert _churn(tmp_path) == {}


# --- hotspots 排序与口径 ---
def test_hotspots_rank_by_churn_times_degree(tmp_path):
    """排序 = churn×degree 交叉积而非 churn 单轴：c.py churn=2 度数=10 score=20 居首；
    a.py churn 同为 2 但度数=1 score=2 居次；b.py score=2 靠 churn 平局裁决垫底."""
    proj = _mk_git_repo(tmp_path)
    _mk_db(proj, {"a.py": 1, "b.py": 2, "c.py": 10})
    r = hotspots(proj, top_n=10)
    assert [h["file"] for h in r["hotspots"]] == ["c.py", "a.py", "b.py"]
    by_file = {h["file"]: h for h in r["hotspots"]}
    assert by_file["c.py"]["score"] == 20 and by_file["c.py"]["churn"] == 2
    assert by_file["c.py"]["degree"] == 10
    assert by_file["a.py"]["score"] == 2 and by_file["b.py"]["score"] == 2
    assert r["git_available"] is True and r["degree_available"] is True


def test_hotspots_excludes_zero_score_and_counts_scanned(tmp_path):
    """零分剔除：d.py 有 churn 无图边 score=0 不入结果，但计入 scanned（参与排序）."""
    proj = _mk_git_repo(tmp_path)
    _mk_db(proj, {"a.py": 1, "b.py": 2, "c.py": 10})
    (proj / "d.py").write_text("w = 1\n", encoding="utf-8")
    _git_run(proj, "add", "."); _git_run(proj, "commit", "-m", "c6")
    r = hotspots(proj, top_n=10)
    assert "d.py" not in [h["file"] for h in r["hotspots"]]
    assert r["scanned"] == 4              # 参与排序 = churn>0 文件数（含零分 d.py）


def test_hotspots_top_n_truncates(tmp_path):
    proj = _mk_git_repo(tmp_path)
    _mk_db(proj, {"a.py": 1, "b.py": 2, "c.py": 10})
    r = hotspots(proj, top_n=2)
    assert len(r["hotspots"]) == 2 and r["hotspots"][0]["file"] == "c.py"


def test_hotspots_negative_top_n_clamped(tmp_path):
    """top_n 负值 -> max(0,·) 钳制（防 [:负数] 切片反转语义截错尾）-> 空结果不炸."""
    proj = _mk_git_repo(tmp_path)
    _mk_db(proj, {"a.py": 1, "b.py": 2, "c.py": 10})
    r = hotspots(proj, top_n=-1)
    assert r["hotspots"] == []


def test_non_git_repo_noop(tmp_path):
    """非 git 仓库 -> git_available=False no-op（无 churn 轴 -> 无热区信息）."""
    r = hotspots(tmp_path)
    assert r["git_available"] is False and r["hotspots"] == [] and r["scanned"] == 0


def test_db_missing_degree_unavailable(tmp_path):
    """git 仓但 codegraph DB 缺失 -> 度数轴缺失 -> 空结果 + degree_available=False
    （churn 单轴无法交叉积，"没有热区信息"≠ok——与 C3 graph_diff 回退同向）."""
    proj = _mk_git_repo(tmp_path)
    r = hotspots(proj)
    assert r["git_available"] is True and r["degree_available"] is False
    assert r["hotspots"] == [] and r["scanned"] == 3


def test_degree_dangling_edge_excluded(tmp_path):
    """dangling 边（source 端点缺失）经 JOIN 剔除——NULL file_path 分组不计数（宁少标：
    无 source 文件的边无处归属，不计入任何文件度数）."""
    proj = _mk_git_repo(tmp_path)
    _mk_db(proj, {"a.py": 1}, dangling=2)
    assert _degree(proj) == {"a.py": 1}


# --- serve 注册（N1 契约）---
def test_serve_registers_hotspots():
    """serve 注册：_SEARCH_TOOLS 登记 + N1 信封装配（检索型三元组，C 信封纪律不走
    override）：有热区 -> ok + confidence=declared；无信号 -> absent."""
    from graphify.serve import _SEARCH_TOOLS, _apply_envelope
    assert "get_hotspots" in _SEARCH_TOOLS
    out = _apply_envelope("get_hotspots", ("Hotspots...", True, 3), freshness="fresh")
    meta = json.loads(out.rstrip("\n").split("\n")[-1].removeprefix("_meta: "))
    assert meta["verdict"] == "ok" and meta["confidence"] == "declared"
    out_absent = _apply_envelope("get_hotspots", ("Hotspots...", False, 0), freshness="fresh")
    meta2 = json.loads(out_absent.rstrip("\n").split("\n")[-1].removeprefix("_meta: "))
    assert meta2["verdict"] == "absent"


def test_format_hotspots_declares_proxies():
    """正文声明 declared 代理（score = churn × 度数，明示非圈复杂度）+ 每行三轴数值."""
    from graphify.serve import _format_hotspots
    r = {"hotspots": [{"file": "graphify/serve.py", "churn": 42, "degree": 310,
                       "score": 13020}],
         "git_available": True, "scanned": 1, "degree_available": True}
    out = _format_hotspots(r)
    assert "declared" in out and "churn × degree" in out
    assert "cyclomatic" in out              # 明示"不是圈复杂度"（declared 纪律）
    assert "churn=42" in out and "degree=310" in out and "score=13020" in out
    assert "graphify/serve.py" in out


def test_format_hotspots_empty_branches():
    """空结果四分支诚实文案——各缺一轴时正文说真话（C3 Imp-1 同款纪律）."""
    from graphify.serve import _format_hotspots
    non_git = _format_hotspots({"hotspots": [], "git_available": False,
                                "scanned": 0, "degree_available": False})
    no_hist = _format_hotspots({"hotspots": [], "git_available": True,
                                "scanned": 0, "degree_available": True})
    no_db = _format_hotspots({"hotspots": [], "git_available": True,
                              "scanned": 3, "degree_available": False})
    no_edges = _format_hotspots({"hotspots": [], "git_available": True,
                                 "scanned": 3, "degree_available": True})
    assert "git unavailable" in non_git
    assert "no commit history" in no_hist
    assert "codegraph DB unavailable" in no_db
    assert "no graph edges" in no_edges
    assert non_git != no_hist != no_db != no_edges   # 四分支互不混淆
