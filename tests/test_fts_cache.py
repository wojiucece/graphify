"""05 票 Seam 2 产物对：graph.json → rebuild_fts 构建与查询行为（快照 + bm25 断言）。

fixture 派生纪律（生产工件派生，禁手写理想化）：AST 节点由真实提取器
（extract_python / extract_js）从 tests/fixtures 源码提取；语义概念节点按生产
graph.json 的语义节点形态（_origin='semantic' / file_type='concept' /
source_location=null）手写最小集（语义提取是 LLM 面，无确定性生产路径）；
列权重探针节点形态 = 提取器输出（六件套字段），内容为测试定制（专测 bm25 列权重
排序）。graph.json 在测试内拼装后写 tmp_path——自包含、随提取器演进自愈。

验收面（05 票）：
- bm25 查询返回与列权重一致（同查询同序可重复）
- camelCase 双向预拆命中（camel ↔ snake）
- 概念节点权重加成；file 节点不进 FTS
- 过滤路径（元数据收窄 + JOIN）
- 缓存删除自动重建 + 指纹命中不重建 + 原子替换
"""
import json, sqlite3, time
from pathlib import Path

import pytest

import fts_cache as fc
from graphify.extract import extract_js, extract_python

FIXTURES = Path(__file__).parent / "fixtures"


# ── fixture 组装（生产派生）───────────────────────────────────────────────────

def _ast_nodes(fixture_name: str, extract_fn) -> list[dict]:
    return extract_fn(FIXTURES / fixture_name)["nodes"]


def _semantic_nodes() -> list[dict]:
    """语义概念节点——形态=生产 graph.json 语义节点（_origin='semantic'、
    file_type='concept'、source_location=null），内容为测试定制。
    'Batch Settlement' 与 dispatchBatch 的 docstring（"...settlement batch..."）
    同词，用于验证概念双列权重加成压过 docstring 单列。"""
    return [
        {"id": "concept:render_loop", "label": "Render Loop", "norm_label": "render loop",
         "file_type": "concept", "_origin": "semantic", "source_file": "docs/concepts",
         "source_location": None},
        {"id": "concept:attention_mechanism", "label": "Attention Mechanism",
         "norm_label": "attention mechanism", "file_type": "concept", "_origin": "semantic",
         "source_file": "docs/concepts", "source_location": None},
        {"id": "concept:batch_settlement", "label": "Batch Settlement",
         "norm_label": "batch settlement", "file_type": "concept", "_origin": "semantic",
         "source_file": "docs/concepts", "source_location": None},
        {"id": "concept:alpha_beta", "label": "Alpha Beta", "norm_label": "alpha beta",
         "file_type": "concept", "_origin": "semantic", "source_file": "docs/concepts",
         "source_location": None},
    ]


def _probe_nodes() -> list[dict]:
    """列权重探针：术语 'alpha' 分别落在不同列（形态=提取器六件套输出）。
    probe_name 的 qn=label（模块级裸名，生产口径），故 alpha 同时命中 name+qn（5）；
    probe_qn 仅 qn（2）；probe_sig 仅 signature（1）；probe_doc 仅 docstring（0.2）。
    另带显式 kind='function' 节点（codegraph 适配器路径形态）测 kind 过滤。"""
    def sym(nid: str, label: str, **kw) -> dict:
        n = {"id": nid, "label": label, "file_type": "code",
             "source_file": "tests/fixtures/weight_probe.py",
             "source_location": "L1:C1", "end_line": 1, "end_byte": 10}
        n.update(kw)
        return n
    return [
        sym("probe_name_alpha", "alpha_widget", qualified_name="alpha_widget"),
        sym("probe_qn_alpha", "widget", qualified_name="Alpha::widget"),
        sym("probe_sig_alpha", "widget", qualified_name="widget",
            signature="(alpha: int) -> void"),
        sym("probe_doc_alpha", "widget", qualified_name="widget",
            docstring="handles the alpha input"),
        # 显式 kind（适配器路径形态）——kind 过滤路径的可测对象
        sym("probe_kind_alpha", "legacy_alpha", kind="function",
            qualified_name="legacy_alpha"),
    ]


def _nodes():
    nodes = _ast_nodes("sample_native_fields.py", extract_python)
    nodes += _ast_nodes("sample_docstrings.ts", extract_js)
    nodes += _semantic_nodes()
    nodes += _probe_nodes()
    return nodes


@pytest.fixture
def graph(tmp_path):
    g = {"directed": False, "multigraph": False, "graph": {},
         "nodes": _nodes(), "links": []}
    out = tmp_path / "graph.json"
    out.write_text(json.dumps(g, ensure_ascii=False), encoding="utf-8")
    return out


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "graphify-out" / ".fts-index.db"


@pytest.fixture
def conn(graph, db_path):
    fc.rebuild_fts(graph, db_path)
    return fc.open_readonly(db_path)


# ── split_identifier（索引/查询共用拆分函数）──────────────────────────────────

def test_split_identifier_boundaries():
    assert fc.split_identifier("pinningSearch") == "pinning Search"  # camel 边界
    assert fc.split_identifier("pinning_search") == "pinning search"  # 下划线
    assert fc.split_identifier("Foo_bar2") == "Foo bar 2"              # 下划线+数字边界
    assert fc.split_identifier("HTTPServer2") == "HTTPServer 2"        # 数字边界（全大写无界）
    assert fc.split_identifier("foo2Bar3") == "foo 2 Bar 3"            # 数字→大写 + 字母→数字
    assert fc.split_identifier("__init__") == "init"
    assert fc.split_identifier("") == ""


# ── schema 快照 ───────────────────────────────────────────────────────────────

def test_schema_snapshot(conn):
    # FTS5 虚表在 sqlite_master 的 type 是 'table'（靠 sql 前缀区分），不能按 type 判
    sql_by_name = {r[0]: r[1] for r in conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE name IN ('nodes','nodes_fts','meta')")}
    assert sql_by_name["nodes"].startswith("CREATE TABLE nodes")
    assert sql_by_name["meta"].startswith("CREATE TABLE meta")
    assert sql_by_name["nodes_fts"].startswith("CREATE VIRTUAL TABLE nodes_fts USING fts5")
    # FTS5 5 列形态（对齐旧链路 nodes_fts）
    cols = [r[1] for r in conn.execute("PRAGMA table_info(nodes_fts)")]
    assert cols == ["id", "name", "qualified_name", "docstring", "signature"]
    # nodes 元数据表 11 列（05 设计 schema）
    ncols = [r[1] for r in conn.execute("PRAGMA table_info(nodes)")]
    assert ncols == ["id", "kind", "name", "qualified_name", "signature", "docstring",
                     "source_file", "source_location", "end_line", "end_byte", "language"]


def test_meta_fingerprint_matches_graph(graph, db_path):
    fc.rebuild_fts(graph, db_path)
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    row = conn.execute("SELECT mtime_ns, size FROM meta").fetchone()
    st = graph.stat()
    assert (row[0], row[1]) == (st.st_mtime_ns, st.st_size)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == fc._SCHEMA_VERSION


def test_build_stats_before_read(graph, db_path):
    """A1（Task 05 二轮评审）：stat 先于 read_text——构建期间 graph.json 被原子替换时，
    指纹记旧值、内容读新值 → 指纹失配自愈（防 is_fresh 永真、陈旧内容不自愈）。
    db_path 父目录须先存在（_build 不 mkdir，rebuild_fts 才 mkdir）。"""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    order = []

    class RecordingPath:
        """Duck-typed graph 路径：委托真实 Path 并记录 stat/read 顺序（_build 只吃
        .stat() 与 .read_text(encoding=...)）。"""
        def stat(self):
            order.append("stat")
            return graph.stat()

        def read_text(self, **kw):
            order.append("read")
            return graph.read_text(**kw)

    fc._build(RecordingPath(), db_path)
    assert order == ["stat", "read"]


# ── 构建正确性：nodes 全量 + FTS 排除 file ───────────────────────────────────

def test_nodes_table_holds_all_nodes(conn, graph):
    g = json.loads(graph.read_text(encoding="utf-8"))
    assert conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == len(g["nodes"])


def test_fts_excludes_file_nodes(conn, graph):
    g = json.loads(graph.read_text(encoding="utf-8"))
    file_ids = {n["id"] for n in g["nodes"]
                if n.get("kind") == "file"
                or fc._is_file_node(n)}
    fts_ids = {r[0] for r in conn.execute("SELECT id FROM nodes_fts")}
    assert file_ids, "fixture 必须含 file 节点"
    assert not (file_ids & fts_ids)          # file 节点一律不进 FTS
    assert len(fts_ids) + len(file_ids) == len(g["nodes"])   # 其余全部进 FTS


def test_fts_excludes_file_node_from_search(graph, db_path, conn):
    # 路径片段 'sample_docstrings' 会命中符号 id（id 列含路径段，旧链路同款行为），
    # 但文件节点本身不在 FTS——其 id 不应出现在任何结果里
    file_ids = [r[0] for r in conn.execute(
        "SELECT id FROM nodes WHERE name = 'sample_docstrings.ts'")]
    assert len(file_ids) == 1
    rows, hits = fc.fts_search(conn, ["sample_docstrings"])
    assert hits >= 1 and rows
    assert file_ids[0] not in {r[0] for r in rows}
    # 文件节点在元数据表（点查面仍在）
    assert conn.execute(
        "SELECT COUNT(*) FROM nodes WHERE id = ?", (file_ids[0],)
    ).fetchone()[0] == 1


# ── bm25 排序与列权重一致（同查询同序可重复）──────────────────────────────────

def test_bm25_column_weight_ordering(conn):
    rows, hits = fc.fts_search(conn, ["alpha"])
    ids = [r[0] for r in rows]
    # 权重档序：name+qn(5) > qn-only(2) > signature(1) > docstring(0.2)。
    # 同档内（5 档三个节点：name 探针 / kind 探针 label 亦含 alpha / 语义双列）得分相等，
    # 次序由 FTS rowid 定，不断言档内顺序——只断权重档相对序（bm25 排序与列权重一致）。
    class5 = {"probe_name_alpha", "probe_kind_alpha", "concept:alpha_beta"}
    pos = {i: nid for i, nid in enumerate(ids)}
    pos_of = {nid: i for i, nid in pos.items()}
    assert set(pos_of) == class5 | {"probe_qn_alpha", "probe_sig_alpha", "probe_doc_alpha"}
    assert all(pos_of[n] < pos_of["probe_qn_alpha"] for n in class5)
    assert pos_of["probe_qn_alpha"] < pos_of["probe_sig_alpha"] < pos_of["probe_doc_alpha"]
    assert hits == 6


def test_same_query_same_order_repeatable(conn):
    first = [r[0] for r in fc.fts_search(conn, ["render"])[0]]
    for _ in range(3):
        again = [r[0] for r in fc.fts_search(conn, ["render"])[0]]
        assert again == first


def test_bm25_weights_translate_ranked_py(conn):
    """bm25 列权重与 ranked.py 逐字一致（id=0/name=3/qn=2/doc=0.2/sig=1）——
    score 的列权重因子直接可验：同术语下 name+qn 命中得分高于 doc 命中。"""
    rows, _ = fc.fts_search(conn, ["alpha"])
    score_by_id = {r[0]: r[4] for r in rows}
    # bm25 越负越好（FTS5 惯例，ORDER BY score ASC = best-first，ranked.py 同款）
    assert score_by_id["probe_name_alpha"] < score_by_id["probe_qn_alpha"] < \
        score_by_id["probe_sig_alpha"] < score_by_id["probe_doc_alpha"]


def test_fts_search_returns_raw_names_not_split(conn):
    """FTS 表存拆段文本，但返回行回填 nodes 原始 label——展示不破损。"""
    rows, _ = fc.fts_search(conn, ["dispatch_batch"])
    assert any(r[0].endswith("dispatchbatch") for r in rows)
    hit = next(r for r in rows if r[0].endswith("dispatchbatch"))
    assert hit[1] == "dispatchBatch()"       # 原始 label，不是 "dispatch Batch()"


# ── camelCase 双向预拆 ───────────────────────────────────────────────────────

def test_camel_query_hits_camel_symbol(conn):
    rows, hits = fc.fts_search(conn, ["dispatchBatch"])
    assert hits >= 1
    assert any(r[0].endswith("dispatchbatch") for r in rows)


def test_snake_query_hits_camel_symbol(conn):
    """反向预拆：snake 查询词命中 camel 符号（设计例 pinningSearch ↔ pinning_search）。"""
    rows, hits = fc.fts_search(conn, ["dispatch_batch"])
    assert hits >= 1
    assert any(r[0].endswith("dispatchbatch") for r in rows)


def test_multiword_camel_query(conn):
    """多段拆词：'renderer' 拆不动（小写连串），'WidgetRenderer' 拆段后可命中类。"""
    rows, hits = fc.fts_search(conn, ["WidgetRenderer"])
    assert hits >= 1
    assert any(r[0].endswith("widgetrenderer") for r in rows)


# ── 语义概念节点：自然语言命中 + 权重加成 ─────────────────────────────────────

def test_semantic_concept_natural_language_query(conn):
    rows, hits = fc.fts_search(conn, ["attention", "mechanism"])
    assert hits >= 1
    assert any(r[0] == "concept:attention_mechanism" for r in rows)


def test_semantic_double_fill_beats_docstring(conn):
    """'settlement'：语义概念（name+qn 双列=5）应压过 dispatchBatch 的 docstring
    （0.2）——概念 label 高信号排前的设计意图。"""
    rows, hits = fc.fts_search(conn, ["settlement"])
    assert rows, "fixture 应含 settlement 命中"
    assert rows[0][0] == "concept:batch_settlement"
    assert any(r[0].endswith("dispatchbatch") for r in rows)   # docstring 也在结果


# ── 过滤路径：元数据表收窄 + JOIN ─────────────────────────────────────────────

def test_filter_by_source_file(conn):
    # 提取器存绝对路径（str(path)），从元数据表取真实值再过滤
    ts_src = conn.execute(
        "SELECT source_file FROM nodes WHERE name = 'sample_docstrings.ts'"
    ).fetchone()[0]
    match = fc._match_expr(["dispatch"])
    rows = fc.filtered_search(conn, match, source_file=ts_src)
    assert rows, "filter 应命中 TS 源符号"
    # 精确断言：所有行都是该源文件符号（元数据表收窄生效）
    for i in {r[0] for r in rows}:
        src = conn.execute("SELECT source_file FROM nodes WHERE id = ?", (i,)).fetchone()[0]
        assert src == ts_src
    # 对照：不过滤时同查询命中跨源（weight_probe + TS）
    all_rows, _ = fc.fts_search(conn, ["widget"])
    assert len({r[0] for r in all_rows}) > len({r[0] for r in rows})


def test_filter_by_kind(conn):
    match = fc._match_expr(["alpha"])
    rows = fc.filtered_search(conn, match, kind="function")
    ids = {r[0] for r in rows}
    assert ids == {"probe_kind_alpha"}        # 仅显式 kind='function' 的节点


def test_filter_combines_kind_and_source_file(conn):
    # probe_kind_alpha 的 label 含 alpha + kind='function'；同源其他 alpha 节点 kind=None
    match = fc._match_expr(["alpha"])
    rows = fc.filtered_search(conn, match, kind="function",
                              source_file="tests/fixtures/weight_probe.py")
    assert rows and all(r[0] == "probe_kind_alpha" for r in rows)


# ── 重建：删除自动重建 / 指纹命中不重建 / 原子替换 ────────────────────────────

def test_delete_cache_rebuilds_automatically(graph, db_path):
    assert fc.ensure_fts(graph, db_path) is True     # 首次构建
    assert db_path.exists()
    db_path.unlink()
    assert not db_path.exists()
    assert fc.ensure_fts(graph, db_path) is True     # 删除后自动重建
    assert db_path.exists()
    conn = fc.open_readonly(db_path)
    assert conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] > 0


def test_fingerprint_hit_does_not_rebuild(graph, db_path):
    assert fc.ensure_fts(graph, db_path) is True
    mtime = db_path.stat().st_mtime_ns
    assert fc.is_fresh(db_path, graph) is True
    assert fc.ensure_fts(graph, db_path) is False    # 指纹命中 → 不重建
    assert db_path.stat().st_mtime_ns == mtime       # 文件未被动过


def test_fingerprint_miss_rebuilds(graph, db_path):
    assert fc.ensure_fts(graph, db_path) is True
    # 事实层变更（内容/尺寸变化）→ 指纹失配 → 重建
    g = json.loads(graph.read_text(encoding="utf-8"))
    g["nodes"].append({"id": "extra_node", "label": "extra_new_symbol",
                       "file_type": "code", "source_file": "x.py",
                       "source_location": "L1:C1", "end_line": 1, "end_byte": 5,
                       "qualified_name": "extra_new_symbol"})
    graph.write_text(json.dumps(g, ensure_ascii=False), encoding="utf-8")
    assert fc.is_fresh(db_path, graph) is False
    assert fc.ensure_fts(graph, db_path) is True
    conn = fc.open_readonly(db_path)
    assert conn.execute("SELECT COUNT(*) FROM nodes WHERE id='extra_node'"
                        ).fetchone()[0] == 1


def test_fingerprint_missing_graph_is_stale(graph, db_path):
    assert fc.ensure_fts(graph, db_path) is True
    graph.unlink()
    assert fc.is_fresh(db_path, graph) is False     # 图缺失 → 诚实 stale


def test_atomic_replace_no_partial_cache(tmp_path, graph, db_path):
    assert fc.ensure_fts(graph, db_path) is True
    before = db_path.read_bytes()
    # 事实层损坏 → rebuild 抛异常，不留半成品（无 tmp 残留、原缓存不动）
    graph.write_text("{ not valid json", encoding="utf-8")
    with pytest.raises(Exception):
        fc.rebuild_fts(graph, db_path)
    assert db_path.exists()
    assert db_path.read_bytes() == before            # 旧缓存完整保留（原子替换）
    leftovers = [p for p in db_path.parent.iterdir()
                 if p.name.startswith(db_path.name + ".") and p.suffix == ".tmp"]
    assert leftovers == []                            # tmp 已清理


def test_atomic_replace_first_build_failure_leaves_no_db(tmp_path):
    graph = tmp_path / "graph.json"
    graph.write_text("{ not valid json", encoding="utf-8")
    db_path = tmp_path / ".fts-index.db"
    with pytest.raises(Exception):
        fc.rebuild_fts(graph, db_path)
    assert not db_path.exists()
    leftovers = [p for p in tmp_path.iterdir()
                 if p.name.startswith(db_path.name + ".") and p.suffix == ".tmp"]
    assert leftovers == []


def test_rebuild_fts_missing_graph_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        fc.rebuild_fts(tmp_path / "nope.json", tmp_path / ".fts-index.db")


# ── 外部内容表同步接口（05 设计留口，对常规 FTS5 表有效）──────────────────────

def test_rebuild_fts_index_sync_command(db_path, conn):
    # 同步接口是写面（watcher 用）——需可写连接；只读 conn 用于验证
    w = sqlite3.connect(str(db_path))
    try:
        fc.rebuild_fts_index(w)                        # 不抛即接口可用
        w.commit()
    finally:
        w.close()
    rows, hits = fc.fts_search(conn, ["render"])       # rebuild 后索引仍有效
    assert hits >= 1
