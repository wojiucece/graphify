"""B1 融合检索：token 分流 / FTS 命中 / pinning / 预算 / query_shape."""
import json, os, sqlite3, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import pytest
from ranked import ranked_context, _count_tokens  # M2: 预算断言与实现同源计数

@pytest.fixture
def mini_db(tmp_path):
    """07 票换源：mini 项目 = graphify-out/graph.json（事实层）+ .fts-index.db（缓存）。
    事实层节点带新链路 AST 符号字段（label/qualified_name/signature/docstring），
    rebuild_fts 投影出 FTS 缓存——ranked_context 的 fts/pinned 查缓存，结构/cjk/gap 查图。"""
    root = tmp_path
    (root / "graphify-out").mkdir()
    graph = {
        "directed": False, "multigraph": False, "graph": {},
        "nodes": [
            # 07 票评审 I2：函数标签用生产形态（带 `()`，graphify label 原生形态）——
            # 让 pinning/检索测试练到真实场景（裸名精确匹配永不命中函数标签）。
            {"id": "a1", "kind": "function", "label": "debounce_grace()",
             "qualified_name": "watch.debounce_grace", "source_file": "watch.py",
             "source_location": "L10:C1", "docstring": "grace window for rebuild",
             "signature": "def debounce_grace(s):"},
            {"id": "a2", "kind": "function", "label": "watch_flush()",
             "qualified_name": "watch.watch_flush", "source_file": "watch.py",
             "source_location": "L30:C1", "docstring": None,
             "signature": "def watch_flush():"},
            {"id": "a3", "kind": "class", "label": "FanoutBase",
             "qualified_name": "pkg.FanoutBase", "source_file": "pkg.py",
             "source_location": "L1:C1", "docstring": None,
             "signature": "class FanoutBase:"},
        ],
        "links": [],
    }
    (root / "graphify-out" / "graph.json").write_text(json.dumps(graph), encoding="utf-8")
    from fts_cache import rebuild_fts
    rebuild_fts(root / "graphify-out" / "graph.json",
                root / "graphify-out" / ".fts-index.db")
    return root

def test_fts_channel_hits_ranked(mini_db):
    r = ranked_context(mini_db, "debounce grace", token_budget=2000)
    assert r["results"], "应有命中"
    assert any("debounce_grace" in x["label"] for x in r["results"])
    assert r["query_shape"]["fts_hits"] >= 1

def test_budget_respected(mini_db):
    # M2：断言用实现同源 _count_tokens（tiktoken 可用时精确计数），不复算 len//4——
    # 装了 tiktoken 的机器不再 flake。
    r = ranked_context(mini_db, "debounce grace", token_budget=50)
    total = sum(_count_tokens(json.dumps(x, ensure_ascii=False))[0] for x in r["results"])
    assert total <= 50

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

def test_pinning_function_label_with_parens(mini_db):
    """07 票评审 I2：函数标签是 `name()` 生产形态（带括号），裸名查询必须仍命中 pinning
    ——predicate 剥括号匹配（lower(name) ∈ {裸名, 裸名+()} 或 qualified_name=裸名）。"""
    r = ranked_context(mini_db, "debounce_grace", token_budget=2000)
    assert r["query_shape"]["pinned"] == ["debounce_grace"]
    assert r["results"][0]["stage"] == "pinned"
    assert any(x["id"] == "a1" for x in r["results"])

def test_gap_hit_linked_to_failed_ref(mini_db):
    """07 票换源：identifier token 与 failed_ref.callee_name 精确匹配（大小写不敏感）
    → gap_hit=true + gap 摘要（替代 codegraph unresolved_refs，匹配语义不变）。"""
    graph = mini_db / "graphify-out" / "graph.json"
    data = json.loads(graph.read_text(encoding="utf-8"))
    data["failed_refs"] = [
        {"from_node": "n1", "callee_name": "debounce_grace", "line": 5, "file_path": "watch.py"}
    ]
    graph.write_text(json.dumps(data), encoding="utf-8")
    r = ranked_context(mini_db, "debounce_grace 处理", token_budget=2000)
    assert r["gap_hit"] is True
    summary = r["query_shape"]["gap_summary"]
    assert summary and summary[0]["ref"] == "debounce_grace"
    assert summary[0]["files"] == ["watch.py:5"]

def test_gap_no_failed_refs_does_not_crash(mini_db):
    """mini 图无 failed_refs → gap 通道诚实空，不崩（R18：没搜到≠不存在）. """
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
    """结构通道：候选 id 在合并图取度数（合并图口径）。"""
    graph = mini_db / "graphify-out" / "graph.json"
    data = json.loads(graph.read_text(encoding="utf-8"))
    data["links"] = [{"source": "a1", "target": "a2"}, {"source": "a1", "target": "a3"}]
    graph.write_text(json.dumps(data), encoding="utf-8")
    r = ranked_context(mini_db, "debounce grace", token_budget=2000)
    assert r["query_shape"]["centrality"] == "merged_graph"
    a1 = next(x for x in r["results"] if x["id"] == "a1")
    assert a1["degree"] == 2

def test_broken_graph_returns_absent(mini_db):
    """07 票：graph.json 损坏（事实层没了）→ M4 诚实 absent + db_missing 标注。旧链路
    raw_db 降级随之退役——graph.json 即事实层，损坏即整条链不可用（无第二个 DB 可回退）。"""
    (mini_db / "graphify-out" / "graph.json").write_text("{broken", encoding="utf-8")
    r = ranked_context(mini_db, "debounce grace", token_budget=2000)
    assert r["results"] == []
    assert r["query_shape"]["db_missing"] is True
    assert r["gap_hit"] is False

def test_collision_suffix_nodes_get_real_degree(mini_db):
    """新链路单 id 池（缓存由 graph.json 投影，__cg 消歧节点即真实节点）——碰撞后缀节点
    度数如实取（join 其自身度数；collision_bases 记录基底供 L1 诊断，不误当真实度 0）。"""
    graph = mini_db / "graphify-out" / "graph.json"
    data = json.loads(graph.read_text(encoding="utf-8"))
    data["nodes"] = [{"id": "a1__cg0", "kind": "function", "label": "debounce_grace()",
                      "qualified_name": "watch.debounce_grace", "source_file": "watch.py",
                      "source_location": "L10:C1", "docstring": None, "signature": ""}]
    data["links"] = []
    graph.write_text(json.dumps(data), encoding="utf-8")
    r = ranked_context(mini_db, "debounce grace", token_budget=2000)
    a1 = next(x for x in r["results"] if x["id"] == "a1__cg0")
    assert a1["degree"] == 0                         # 孤立节点，度数为真实 0

def test_count_mode_reported(mini_db):
    """tiktoken 可选（可用则精确）否则 len//4 估算——query_shape.count_mode 报告."""
    r = ranked_context(mini_db, "debounce grace", token_budget=50)
    assert r["query_shape"]["count_mode"] in ("tiktoken", "estimate")

def test_graph_cache_reuses_between_queries(mini_db):
    """M1：同 root 连续两次查询，第二次命中 mtime+size 缓存（不重复 json.loads 全量图）."""
    import ranked
    ranked._graph_loads = 0
    ranked_context(mini_db, "debounce grace", token_budget=2000)
    loads_first = ranked._graph_loads
    ranked_context(mini_db, "debounce grace", token_budget=2000)
    assert ranked._graph_loads == loads_first == 1, "第二次查询应命中缓存（loads 不增）"

def test_graph_cache_invalidates_on_utime_change(mini_db):
    """M1 失效：同 root 查询后 os.utime 改 mtime（size 不变）→ 再查询触发重载（loads=2）.
    rebuild 后 mtime 必变（Windows st_mtime_ns ~100ns 粒度远小于 rebuild 间隔），
    此断言锁定"mtime 变 → 缓存必失效"这一失效语义。"""
    import ranked
    import time
    ranked._graph_loads = 0
    ranked_context(mini_db, "debounce grace", token_budget=2000)
    assert ranked._graph_loads == 1
    graph = mini_db / "graphify-out" / "graph.json"
    t = time.time_ns()
    os.utime(graph, ns=(t + 10**9, t + 10**9))
    ranked_context(mini_db, "debounce grace", token_budget=2000)
    assert ranked._graph_loads == 2, "mtime 变更应失效缓存（重载全量图）"

def test_missing_graph_returns_absent_not_error(tmp_path):
    """M4：非 graphify 项目（无 graphify-out/graph.json，多项目热切换目标）→
    absent + db_missing 标注，不 isError（真错误仅限事实层在但查询炸）. """
    from ranked import format_ranked
    from graphify.serve import _apply_envelope
    r = ranked_context(tmp_path, "any query", token_budget=2000)   # tmp_path 无 graphify-out
    assert r["results"] == []
    assert r["query_shape"]["db_missing"] is True
    assert r["query_shape"]["scanned"] == 0
    assert r["gap_hit"] is False
    # N1 信封：found=False + scanned=0 → absent（body 带 db_missing 标注区分于空图）
    out = _apply_envelope("get_ranked_context",
                          (format_ranked(r), False, 0), freshness="fresh")
    meta = json.loads(out.rstrip("\n").split("\n")[-1].removeprefix("_meta: "))
    assert meta["verdict"] == "absent"

def test_cache_missing_falls_back_to_graph_channels(mini_db, monkeypatch):
    """07 票降级：缓存不可用（fts_cache 模块缺失）→ 回退纯图通道——cjk/structure/gap
    照常（事实层在），fts/pinned 诚实零命中 + cache_missing 标注（不逃逸 ImportError）。"""
    import ranked
    monkeypatch.setattr(ranked, "_fts_cache", lambda: None)
    graph = mini_db / "graphify-out" / "graph.json"
    data = json.loads(graph.read_text(encoding="utf-8"))
    data["nodes"].append({"id": "concept:1", "label": "防抖窗口"})
    data["failed_refs"] = [
        {"from_node": "n1", "callee_name": "debounce_grace", "line": 5, "file_path": "watch.py"}
    ]
    graph.write_text(json.dumps(data), encoding="utf-8")
    r = ranked_context(mini_db, "防抖 debounce_grace", token_budget=2000)
    assert r["query_shape"]["cache_missing"] is True
    assert r["query_shape"]["cjk_hits"] == 1
    assert r["gap_hit"] is True
    assert all(x["stage"] == "cjk" for x in r["results"])

def test_camel_case_query_hits_snake_symbol(mini_db):
    """05 camel 双侧预拆：查询侧 'debounceGrace' 拆段后命中索引侧 'debounce_grace'
    （拆段 'debounce grace'）——camelCase ↔ snake_case 互相命中。"""
    r = ranked_context(mini_db, "debounceGrace", token_budget=2000)
    assert r["query_shape"]["fts_hits"] >= 1
    assert any(x["id"] == "a1" for x in r["results"])

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

# M3：金标根支持 env 覆盖（GRAPHIFY_GOLDEN_ROOT），默认保留本机 D:/code/graphify_fork；
# DB 缺失时的 skip 由 tests/conftest.py 的 golden gate（autouse fixture）统一处理——
# 闸门存在性对 `-m golden` 可见，'skipped (golden)' 进测试摘要，不静默消失。
_FORK_ROOT = Path(os.environ.get("GRAPHIFY_GOLDEN_ROOT", r"D:/code/graphify_fork"))


def _load_golden() -> list[dict]:
    return json.loads(
        Path(__file__).parent.joinpath("fixtures/ranked_golden.json").read_text(encoding="utf-8"))


def _recall(results: list[dict], expect: list[str]) -> float:
    top5 = [x["id"] for x in results[:5]]
    hit = sum(1 for eid in expect if eid in top5)
    return hit / len(expect)


@pytest.mark.golden
def test_golden_matrix_recall_and_no_degrade():
    """金标 20 条（spec 验收阈值 95%，5% 容差给 graphify↔codegraph 上游提取覆盖差异）：
    单查询 pass = expect 至少一个命中 top5；整体通过率 ≥ 95%；且融合 ≥ BM25-only 对照
    （不降）；1000/2000 两档预算。DB 缺失时由 conftest golden gate 跳过
    （'skipped (golden)' 进摘要）."""
    golden = _load_golden()
    assert len(golden) == 20, "第一批矩阵最小集 10 条 + 第二批 querylog 替代语料 10 条 = 20 条"
    for budget in (1000, 2000):
        fusion_passes = bm25_passes = 0
        rows = []
        for item in golden:
            r_f = ranked_context(_FORK_ROOT, item["q"], token_budget=budget)
            r_b = ranked_context(_FORK_ROOT, item["q"], token_budget=budget, channels={"fts"})
            f_pass = _recall(r_f["results"], item["expect"]) > 0
            b_pass = _recall(r_b["results"], item["expect"]) > 0
            fusion_passes += f_pass
            bm25_passes += b_pass
            rows.append((item["q"], f_pass, b_pass, r_f["gap_hit"]))
        f_rate = fusion_passes / len(golden)
        b_rate = bm25_passes / len(golden)
        assert f_rate >= 0.95, f"budget={budget} 融合通过率 {f_rate:.2f} < 95%: {rows}"
        assert f_rate >= b_rate, f"budget={budget} 融合 {f_rate:.2f} 劣于 BM25-only {b_rate:.2f}: {rows}"


@pytest.mark.golden
def test_golden_gap_queries_report_gap_hit():
    """金标 gap 型 2 条：identifier 命中 gap.ref → gap_hit=true（方案 §4-B1 联动）.
    DB 缺失时由 conftest golden gate 跳过（'skipped (golden)' 进摘要）."""
    for item in _load_golden():
        if item.get("gap"):
            r = ranked_context(_FORK_ROOT, item["q"], token_budget=2000)
            assert r["gap_hit"] is True, f"gap 型查询 {item['q']!r} 应 gap_hit=true"
            assert r["query_shape"]["gap_summary"], f"gap 型查询 {item['q']!r} 应有 gap 摘要"
