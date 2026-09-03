"""C4：hotspots = churn（git log 文件 commit 频次）× 度数代理（graph.json 合并图边
双端点 source_file 计数）top-N.

06 票换源：codegraph DB 退役，度数轴取 graph.json 唯一事实层（合并图边计数，与
god_nodes/B1 同单位——旧链路 raw 边计数单位差异按决议诚实标注，见 _format_hotspots
头行断言）。
覆盖：churn 计数（含中文文件名 quotepath）、churn×degree 交叉积排序（非 churn 单轴）、
零分剔除与 scanned 口径、top_n 钳制、非 git no-op、graph.json 缺失度数轴缺失、dangling
边剔除、serve 注册（_SEARCH_TOOLS + N1 信封，C 信封纪律不走 override）、正文 declared
代理声明、空结果四分支诚实文案。
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
    .git/info/exclude 加 graphify-out/ + .codegraph/（生产仓 .gitignore 效果同源——
    否则 _mk_graph 的 graph.json 会被 git add . 提交进历史污染 churn；exclude 不进 git
    历史，不产生额外 churn 文件）。"""
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git_run(tmp_path, "init"); _git_run(tmp_path, "config", "user.email", "t@t")
    _git_run(tmp_path, "config", "user.name", "t")
    (tmp_path / ".git" / "info").mkdir(exist_ok=True)   # git init 已建 info/（含样例）
    (tmp_path / ".git" / "info" / "exclude").write_text("graphify-out/\n.codegraph/\n",
                                                        encoding="utf-8")
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


def _mk_graph(root, degrees, dangling=0, fan_in=None):
    """最小 graph.json（新链路唯一事实层）：nodes + links 与生产形态同形（test_git_symbols
    同口径）.

    _degree 引用 nodes(id, source_file) + links(source, target)——fixture 按引用字段
    同形创建（禁手写理想化）。
    degrees={file: 出边数}（source 端，target 用虚构 t{n}——不在 nodes，仅 source 端计入）；
    fan_in={file: 入边数}（I1 双端语义：target 端，source 用真实插入的 xsrc 虚节点
    ——端点须在 nodes 才有归属，xsrc 节点自身无边不进度数表）；
    dangling=两端点都不在 nodes 的边数（两端都不计入）。"""
    out = root / "graphify-out"; out.mkdir(exist_ok=True)
    nodes, links = [], []
    n = 0
    for file, deg in degrees.items():
        nid = f"id:{file}"
        nodes.append({"id": nid, "label": f"f.{file}", "qualified_name": f"f.{file}",
                      "source_file": file})
        for _ in range(deg):
            n += 1
            links.append({"source": nid, "target": f"t{n}", "relation": "calls"})
    for i in range(dangling):
        n += 1
        links.append({"source": f"ghost{i}", "target": f"t{n}", "relation": "calls"})
    for file, cnt in (fan_in or {}).items():
        tid = f"id:{file}"
        if not any(x["id"] == tid for x in nodes):
            nodes.append({"id": tid, "label": f"f.{file}", "qualified_name": f"f.{file}",
                          "source_file": file})
        for j in range(cnt):
            n += 1
            src = f"id:xsrc{j}_{file}"
            nodes.append({"id": src, "label": f"x.{file}", "qualified_name": f"x.x{j}",
                          "source_file": f"xsrc{j}_{file}.py"})
            links.append({"source": src, "target": tid, "relation": "imports"})
    g = {"directed": False, "multigraph": False, "graph": {},
         "nodes": nodes, "links": links}
    (out / "graph.json").write_text(json.dumps(g), encoding="utf-8")


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
    a.py churn 同为 2、度数=1 出边+4 入边=5（I1 双端计数）score=10 居次；b.py score=2
    靠 churn 平局裁决垫底."""
    proj = _mk_git_repo(tmp_path)
    _mk_graph(proj, {"a.py": 1, "b.py": 2, "c.py": 10}, fan_in={"a.py": 4})
    r = hotspots(proj, top_n=10)
    assert [h["file"] for h in r["hotspots"]] == ["c.py", "a.py", "b.py"]
    by_file = {h["file"]: h for h in r["hotspots"]}
    assert by_file["c.py"]["score"] == 20 and by_file["c.py"]["churn"] == 2
    assert by_file["c.py"]["degree"] == 10
    # a.py 度数 = 出边 1 + 入边 4 = 5（in+out 双端计数——source-only 实现下此处 degree=1
    # score=2 与 b.py 平局、断言红：fixture 对 BOTH 语义有区分度）
    assert by_file["a.py"]["degree"] == 5
    assert by_file["a.py"]["score"] == 10 and by_file["b.py"]["score"] == 2
    assert r["git_available"] is True and r["degree_available"] is True


def test_hotspots_excludes_zero_score_and_counts_scanned(tmp_path):
    """零分剔除：d.py 有 churn 无图边 score=0 不入结果，但计入 scanned（参与排序）."""
    proj = _mk_git_repo(tmp_path)
    _mk_graph(proj, {"a.py": 1, "b.py": 2, "c.py": 10})
    (proj / "d.py").write_text("w = 1\n", encoding="utf-8")
    _git_run(proj, "add", "."); _git_run(proj, "commit", "-m", "c6")
    r = hotspots(proj, top_n=10)
    assert "d.py" not in [h["file"] for h in r["hotspots"]]
    assert r["scanned"] == 4              # 参与排序 = churn>0 文件数（含零分 d.py）


def test_hotspots_top_n_truncates(tmp_path):
    proj = _mk_git_repo(tmp_path)
    _mk_graph(proj, {"a.py": 1, "b.py": 2, "c.py": 10})
    r = hotspots(proj, top_n=2)
    assert len(r["hotspots"]) == 2 and r["hotspots"][0]["file"] == "c.py"


def test_hotspots_negative_top_n_clamped(tmp_path):
    """top_n 负值 -> max(0,·) 钳制（防 [:负数] 切片反转语义截错尾）-> 空结果不炸."""
    proj = _mk_git_repo(tmp_path)
    _mk_graph(proj, {"a.py": 1, "b.py": 2, "c.py": 10})
    r = hotspots(proj, top_n=-1)
    assert r["hotspots"] == []


def test_non_git_repo_noop(tmp_path):
    """非 git 仓库 -> git_available=False no-op（无 churn 轴 -> 无热区信息）."""
    r = hotspots(tmp_path)
    assert r["git_available"] is False and r["hotspots"] == [] and r["scanned"] == 0


def test_graph_missing_degree_unavailable(tmp_path):
    """git 仓但 graph.json 缺失 -> 度数轴缺失 -> 空结果 + degree_available=False
    （churn 单轴无法交叉积，"没有热区信息"≠ok——与 C3 graph_diff 回退同向）."""
    proj = _mk_git_repo(tmp_path)
    r = hotspots(proj)
    assert r["git_available"] is True and r["degree_available"] is False
    assert r["hotspots"] == [] and r["scanned"] == 3


def test_degree_dangling_edge_excluded(tmp_path):
    """dangling 边（两端点都缺失）双端 JOIN 均剔除——不计入任何文件度数（宁少标：
    无真实端点文件的边无处归属）。I1 后按双端语义：source/target 两段各自 INNER JOIN，
    两端都不存在 -> 两段都不产生该边计数."""
    proj = _mk_git_repo(tmp_path)
    _mk_graph(proj, {"a.py": 1}, dangling=2)
    assert _degree(proj) == {"a.py": 1}


def test_hotspots_high_fan_in_low_fan_out_is_hotspot(tmp_path):
    """I1（owner 审核）：fan-in 纳入度数——models.py 被多文件 import（高 fan-in）
    但自己不调用别人（0 出边）也应为热区（"常改 × 波及大"的波及=别人依赖你）。
    source-only 实现下 models.py degree=0 永不成热区（方向反了）——本用例对其红."""
    proj = _mk_git_repo(tmp_path)
    (proj / "models.py").write_text("K = 1\n", encoding="utf-8")
    _git_run(proj, "add", "."); _git_run(proj, "commit", "-m", "c6")   # models.py churn=1
    _mk_graph(proj, {"a.py": 1, "b.py": 2, "c.py": 10}, fan_in={"models.py": 3})
    r = hotspots(proj, top_n=10)
    by_file = {h["file"]: h for h in r["hotspots"]}
    assert "models.py" in by_file          # source-only 下不在结果（degree=0 score=0）
    assert by_file["models.py"]["degree"] == 3 and by_file["models.py"]["score"] == 3
    assert by_file["models.py"]["churn"] == 1
    # 排序：c(20) > models(3) > a(2)=b(2)——fan-in 文件进热区前列
    assert [h["file"] for h in r["hotspots"]][:2] == ["c.py", "models.py"]


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


def test_parse_top_n_defensive():
    """Minor-2：top_n 非法（缺失/非数/None）回退缺省 10 不炸——MCP schema enum
    [5,10,20,50] 之外的防御层（客户端不守 enum / handler 直调时不抛 ValueError）."""
    from graphify.serve import _parse_top_n
    assert _parse_top_n({}) == 10
    assert _parse_top_n({"top_n": 20}) == 20
    assert _parse_top_n({"top_n": "50"}) == 50
    assert _parse_top_n({"top_n": "abc"}) == 10
    assert _parse_top_n({"top_n": None}) == 10


def test_format_hotspots_declares_proxies():
    """正文声明 declared 代理（score = churn × 度数，明示非圈复杂度）+ 每行三轴数值.
    06 票：degree 单位 = graph.json 合并图边计数（与 god_nodes/B1 同单位）——诚实标注
    旧链路 raw 边计数单位差异。"""
    from graphify.serve import _format_hotspots
    r = {"hotspots": [{"file": "graphify/serve.py", "churn": 42, "degree": 310,
                       "score": 13020}],
         "git_available": True, "scanned": 1, "degree_available": True}
    out = _format_hotspots(r)
    assert "declared" in out and "churn × degree" in out
    assert "cyclomatic" in out              # 明示"不是圈复杂度"（declared 纪律）
    assert "merged-graph" in out and "god_nodes" in out   # 度数单位诚实标注（06 决议）
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
    assert "graph.json unavailable" in no_db
    assert "no graph edges" in no_edges
    assert non_git != no_hist != no_db != no_edges   # 四分支互不混淆
