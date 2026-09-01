"""B1 融合检索：token 分流 / FTS 命中 / pinning / 预算 / query_shape."""
import json, sqlite3, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import pytest
from ranked import ranked_context

@pytest.fixture
def mini_db(tmp_path):
    root = tmp_path; db = root / ".codegraph" / "codegraph.db"
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY, kind TEXT, name TEXT, "
                 "qualified_name TEXT, file_path TEXT, language TEXT, start_line INT, "
                 "end_line INT, docstring TEXT, signature TEXT)")
    conn.execute("CREATE VIRTUAL TABLE nodes_fts USING fts5(id, name, qualified_name, "
                 "docstring, signature, content='nodes', content_rowid='rowid')")
    conn.execute("CREATE TABLE edges (id INTEGER PRIMARY KEY, source TEXT, target TEXT, "
                 "kind TEXT, metadata TEXT, line INT, col INT, provenance TEXT)")
    rows = [
        ("a1", "function", "debounce_grace", "watch.debounce_grace", "watch.py", "python", 10, 20,
         "grace window for rebuild", "def debounce_grace(s):"),
        ("a2", "function", "watch_flush", "watch.watch_flush", "watch.py", "python", 30, 40,
         None, "def watch_flush():"),
        ("a3", "class", "FanoutBase", "pkg.FanoutBase", "pkg.py", "python", 1, 99, None, "class FanoutBase:"),
    ]
    conn.executemany("INSERT INTO nodes VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    conn.execute("INSERT INTO nodes_fts(nodes_fts) VALUES('rebuild')")
    for r in rows:
        conn.execute("INSERT INTO nodes_fts(rowid, id, name, qualified_name, docstring, signature) "
                     "SELECT rowid, id, name, qualified_name, docstring, signature FROM nodes WHERE id=?",(r[0],))
    conn.commit(); conn.close()
    (root / "graphify-out").mkdir()
    (root / "graphify-out" / "graph.json").write_text(json.dumps(
        {"directed": False, "multigraph": False, "graph": {}, "nodes": [], "links": []}), encoding="utf-8")
    return root

def test_fts_channel_hits_ranked(mini_db):
    r = ranked_context(mini_db, "debounce grace", token_budget=2000)
    assert r["results"], "应有命中"
    assert any("debounce_grace" in x["label"] for x in r["results"])
    assert r["query_shape"]["fts_hits"] >= 1

def test_budget_respected(mini_db):
    r = ranked_context(mini_db, "debounce grace", token_budget=50)
    assert sum(len(json.dumps(x, ensure_ascii=False)) // 4 for x in r["results"]) <= 50

def test_query_shape_reports_cjk(mini_db):
    r = ranked_context(mini_db, "防抖")   # 纯 CJK，FTS 零命中
    assert r["query_shape"]["cjk_hits"] == 0 and "results" in r   # 显式报告不装不存在

# === 追加：B1 其余通道 / 预算 / 服务注册 / 金标（矩阵最小集）==========================

def test_token_split_identifier_vs_cjk(mini_db):
    """Q8 混合查询路由：identifier tokens 进 FTS，CJK tokens 进图侧子串，query_shape 报告双通道命中."""
    r = ranked_context(mini_db, "watch debounce_grace 防抖", token_budget=2000)
    shape = r["query_shape"]
    assert "watch" in shape["tokens"]["identifier"]
    assert "debounce_grace" in shape["tokens"]["identifier"]
    assert "防抖" in shape["tokens"]["cjk"]
    assert shape["fts_hits"] >= 1
    assert shape["cjk_hits"] == 0          # 空图：显式报告零命中
    assert shape["scanned"] >= 1           # 候选池大小（N1 scanned）

def test_pinning_exact_name_ranks_top(mini_db):
    """source-shaped token（PascalCase FanoutBase）→ 精确名 pinning，top 结果带 stage 归因."""
    r = ranked_context(mini_db, "FanoutBase", token_budget=2000)
    assert r["query_shape"]["pinned"] == ["FanoutBase"]
    assert r["results"][0]["stage"] == "pinned"
    assert any(x["label"] == "pkg.FanoutBase" for x in r["results"])

def test_gap_hit_linked_to_unresolved_ref(mini_db):
    """identifier token 与 gap.ref 精确匹配（大小写不敏感）→ gap_hit=true + gap 摘要."""
    conn = sqlite3.connect(mini_db / ".codegraph" / "codegraph.db")
    conn.execute("CREATE TABLE unresolved_refs (from_node_id TEXT, reference_name TEXT, "
                 "line INT, file_path TEXT, status TEXT)")
    conn.execute("INSERT INTO unresolved_refs VALUES ('n1', 'debounce_grace', 5, 'watch.py', 'failed')")
    conn.commit(); conn.close()
    r = ranked_context(mini_db, "debounce_grace 处理", token_budget=2000)
    assert r["gap_hit"] is True
    summary = r["query_shape"]["gap_summary"]
    assert summary and summary[0]["ref"] == "debounce_grace"
    assert summary[0]["files"] == ["watch.py:5"]

def test_gap_no_table_does_not_crash(mini_db):
    """mini_db 无 unresolved_refs 表 → gap 通道诚实空，不崩（R18：没搜到≠不存在）. """
    r = ranked_context(mini_db, "debounce grace", token_budget=2000)
    assert r["gap_hit"] is False
    assert r["query_shape"]["gap_summary"] == []

def test_cjk_channel_substring_matches_graph_label(mini_db):
    """CJK 通道：合并图 label 子串匹配（O(n)，语义节点规模可行）."""
    graph = mini_db / "graphify-out" / "graph.json"
    graph.write_text(json.dumps({"directed": False, "multigraph": False, "graph": {},
        "nodes": [{"id": "concept:1", "label": "防抖窗口"},
                  {"id": "concept:2", "label": "降级策略"}],
        "links": []}), encoding="utf-8")
    r = ranked_context(mini_db, "防抖", token_budget=2000)
    assert r["query_shape"]["cjk_hits"] == 1
    assert any("防抖窗口" in x["label"] for x in r["results"])
    assert r["results"][0]["stage"] == "cjk"

def test_structural_degree_reported_from_merged_graph(mini_db):
    """结构通道：候选 id 在合并图取度数（合并图口径，非 DB GROUP BY）."""
    graph = mini_db / "graphify-out" / "graph.json"
    graph.write_text(json.dumps({"directed": False, "multigraph": False, "graph": {},
        "nodes": [{"id": "a1"}, {"id": "a2"}, {"id": "a3"}],
        "links": [{"source": "a1", "target": "a2"}, {"source": "a1", "target": "a3"}]}),
        encoding="utf-8")
    r = ranked_context(mini_db, "debounce grace", token_budget=2000)
    assert r["query_shape"]["centrality"] == "merged_graph"
    a1 = next(x for x in r["results"] if x["id"] == "a1")
    assert a1["degree"] == 2

def test_centrality_degrades_to_raw_db_on_bad_graph(mini_db):
    """graph.json 损坏 → 结构通道降级 DB edges GROUP BY，centrality=raw_db 标注."""
    (mini_db / "graphify-out" / "graph.json").write_text("{broken", encoding="utf-8")
    r = ranked_context(mini_db, "debounce grace", token_budget=2000)
    assert r["query_shape"]["centrality"] == "raw_db"
    assert r["results"]                              # 降级不毁响应

def test_collision_nodes_skip_structural_join(mini_db):
    """id 碰撞（_normalize_id fold 含 __cg 消歧后缀）→ 不参与 join，_meta 记录 collisions."""
    graph = mini_db / "graphify-out" / "graph.json"
    graph.write_text(json.dumps({"directed": False, "multigraph": False, "graph": {},
        "nodes": [{"id": "a1__cg0"}],
        "links": []}), encoding="utf-8")
    r = ranked_context(mini_db, "debounce grace", token_budget=2000)
    assert r["query_shape"]["collisions"] >= 1
    a1 = next(x for x in r["results"] if x["id"] == "a1")
    assert a1["degree"] == 0                         # 未 join，度数缺失

def test_count_mode_reported(mini_db):
    """tiktoken 可选（可用则精确）否则 len//4 估算——query_shape.count_mode 报告."""
    r = ranked_context(mini_db, "debounce grace", token_budget=50)
    assert r["query_shape"]["count_mode"] in ("tiktoken", "estimate")

def test_format_ranked_wraps_meta(mini_db):
    """B1 是 JSON 工具：format_ranked 序列化体含 _meta 对象（query_shape/gap_hit），
    与 serve 尾部 _meta 信封行（verdict/freshness）并列不冲突."""
    from ranked import format_ranked
    r = ranked_context(mini_db, "debounce grace", token_budget=2000)
    body = json.loads(format_ranked(r))
    assert "results" in body and "_meta" in body
    assert body["_meta"]["query_shape"]["fts_hits"] >= 1
    assert "gap_hit" in body["_meta"]

def test_serve_registers_ranked_context():
    """serve 注册：登记 _SEARCH_TOOLS + N1 信封装配（检索型三元组）."""
    from graphify.serve import _SEARCH_TOOLS, _apply_envelope
    assert "get_ranked_context" in _SEARCH_TOOLS
    out = _apply_envelope("get_ranked_context", ("{}", True, 3), freshness="fresh")
    meta = json.loads(out.rstrip("\n").split("\n")[-1].removeprefix("_meta: "))
    assert meta["verdict"] == "ok"
    out_absent = _apply_envelope("get_ranked_context", ("{}", False, 0), freshness="fresh")
    meta2 = json.loads(out_absent.rstrip("\n").split("\n")[-1].removeprefix("_meta: "))
    assert meta2["verdict"] == "absent" and meta2["empty_graph"] is True

# === 金标集（Q-2 拆批：第一批 = 10 条矩阵最小集）=====================================
# expect 符号 id 从 fork 真实图（D:/code/graphify_fork）派生（fixture 生产派生纪律），
# 只读验证：无真实图时跳过（不 gate 合成 fixture 的单测）。融合 vs BM25-only 对照不降。

_FORK_ROOT = Path(r"D:/code/graphify_fork")


def _load_golden() -> list[dict]:
    return json.loads(
        Path(__file__).parent.joinpath("fixtures/ranked_golden.json").read_text(encoding="utf-8"))


def _recall(results: list[dict], expect: list[str]) -> float:
    top5 = [x["id"] for x in results[:5]]
    hit = sum(1 for eid in expect if eid in top5)
    return hit / len(expect)


def test_golden_matrix_recall_and_no_degrade():
    """金标 10 条：命中@5 ≥ 50%，且融合 ≥ BM25-only 对照（不降）；1000/2000 两档预算."""
    if not (_FORK_ROOT / ".codegraph" / "codegraph.db").exists():
        pytest.skip("fork 真实图不存在（只读验证依赖 D:/code/graphify_fork）")
    golden = _load_golden()
    assert len(golden) == 10, "第一批矩阵最小集 = 10 条"
    for budget in (1000, 2000):
        fusion_hits = bm25_hits = 0.0
        rows = []
        for item in golden:
            r_f = ranked_context(_FORK_ROOT, item["q"], token_budget=budget)
            r_b = ranked_context(_FORK_ROOT, item["q"], token_budget=budget, channels={"fts"})
            f_recall = _recall(r_f["results"], item["expect"])
            b_recall = _recall(r_b["results"], item["expect"])
            fusion_hits += f_recall
            bm25_hits += b_recall
            rows.append((item["q"], f_recall, b_recall, r_f["gap_hit"]))
        f_total = fusion_hits / len(golden)
        b_total = bm25_hits / len(golden)
        assert f_total >= 0.5, f"budget={budget} 融合命中@5 {f_total:.2f} < 50%: {rows}"
        assert f_total >= b_total, f"budget={budget} 融合 {f_total:.2f} 劣于 BM25-only {b_total:.2f}: {rows}"


def test_golden_gap_queries_report_gap_hit():
    """金标 gap 型 2 条：identifier 命中 gap.ref → gap_hit=true（方案 §4-B1 联动）. """
    if not (_FORK_ROOT / ".codegraph" / "codegraph.db").exists():
        pytest.skip("fork 真实图不存在（只读验证依赖 D:/code/graphify_fork）")
    for item in _load_golden():
        if item.get("gap"):
            r = ranked_context(_FORK_ROOT, item["q"], token_budget=2000)
            assert r["gap_hit"] is True, f"gap 型查询 {item['q']!r} 应 gap_hit=true"
            assert r["query_shape"]["gap_summary"], f"gap 型查询 {item['q']!r} 应有 gap 摘要"
