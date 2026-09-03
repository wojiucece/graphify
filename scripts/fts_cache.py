"""05 票：FTS 缓存模块——从 graph.json 落盘可重建的全文检索缓存。

graphify-out/.fts-index.db（隐藏，事实层派生缓存，删除无损）三表：

  nodes      元数据表（全量节点，含 file/语义节点——服务点查类工具与过滤路径）
  nodes_fts  FTS5 表，5 列 id/name/qualified_name/docstring/signature；bm25 权重
             (0,3,2,0.2,1) 与默认 tokenizer（unicode61）逐字对齐旧链路 ranked.py
             _fts_search，保证排序等价迁移（02 票 bm25 等价性结论）
  meta       graph.json (mtime_ns, size) 指纹 + built_at；user_version 记 schema
             版本——serve 重启时判缓存可复用（指纹命中不重建）

设计来源：docs/graphify-native-indexing-spec.md §FTS 缓存 + wayfinder/tickets/05。

【核心设计判断：camel 预拆 × 存储形态（对 05 设计 schema 的一处有据偏差）】
05 设计 schema 写 nodes_fts 为"外部内容表 content='nodes'"。但 content='nodes' 的
FTS 索引由 content 表列文本重建，索引文本 == content 文本——而 camel 预拆（验收红线：
camelCase 与 snake_case 互相命中）要求 FTS 索引拿到拆段 token（"pinning Search"），
同一列同时存原始文本（get_node 点查要展示 pinningSearch）不可能。二选一必损其一：
  (a) 强用 content='nodes' → nodes.name/qn 被迫存拆段文本 → get_node/pinning 展示
      破损名（回归）；
  (b) 本模块所选：nodes_fts 用常规独立 FTS5 表（无 content=），索引存拆段文本，
      nodes 元数据表存原始文本，rowid 对齐后 JOIN。snippet/bm25/MATCH 行为与外部
      内容表完全一致（同为 FTS5 标准虚表），且 FTS 索引随 INSERT/UPDATE 自动同步
      （比外部内容表的手动 rebuild 更适合 04 票 watcher 的局部更新意图）。
偏差影响面：02 票 bm25 等价性结论只要求同 5 列、同列权重、同 tokenizer（unicode61）
——全部保留，与虚表由谁构建无关；"外部内容表同步接口"以 rebuild_fts_index() 保留
（'rebuild' 命令对常规 FTS5 表同样有效，实测 OK），watcher 局部更新另有更简单的
自动同步路径。

范围纪律：本模块只做"缓存构建 + 构建行为的可测查询面"。消费侧（ranked.py 换源、
serve 惰性触发、watcher 进程内直通）是 06/10 票的事；这里暴露的查询函数
（fts_search / filtered_search / open_readonly / split_identifier）是给消费侧留的
接口，ranked.py 的 _fts_search 逐字平移到 fts_search（同 5 列、同权重、同 snippet
列 3、同 MATCH 隐式 AND、同 LIMIT 40）。failed_refs 不接（07 gap 通道消费，
05 只做三表）。
"""
from __future__ import annotations
import json, os, re, sqlite3, sys, tempfile, time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:            # import graphify（本地包，不依赖安装态）
    sys.path.insert(0, str(_ROOT))

# 分类判定复用 graphify 既有实现（graphify/build.py）：
#   _is_ast_tier        AST vs 语义 tier（_origin 优先，缺省按 source_location ^L\d）
#   _is_file_node_label label 是否为 source_file 的文件节点 label（裸 basename 或
#                       directory-qualified 后缀）
# 与生产分类同源，避免 FTS 缓存与 graphify 自身对节点 tier 的判定漂移。二者是 build
# 的私有函数——上游若改名/移动，check-custom.sh 应登记守护（同 ranked.py import 先例）。
from graphify.build import _is_ast_tier, _is_file_node_label

_SCHEMA_VERSION = 1
_FTS_LIMIT = 40          # 对齐 ranked.py _FTS_LIMIT（候选池上限）
# bm25 列权重（id/name/qualified_name/docstring/signature）——ranked.py 逐字平移：
# id 权重 0 是设计选择（id 是 raw hash 无语义，匹配无检索价值），非漏参。
_BM25_WEIGHTS = (0, 3, 2, 0.2, 1)
_BM25_SQL = "bm25(nodes_fts, 0, 3, 2, 0.2, 1)"
# snippet 只用于 docstring 列（列下标 3）——name/qualified_name 列已 camel 预拆，
# 高亮在拆段文本上会失真，docstring 是自然文本故无此风险（05 设计决议）。
_SNIPPET_SQL = "COALESCE(snippet(nodes_fts, 3, '', '', '…', 8), '')"

# ── camelCase 预拆（索引侧与查询侧共用同一函数）──────────────────────────────
# 规则（05 设计）：camel 边界 / 下划线（含连字符、空白）/ 数字边界 → 空格。
# 例：pinningSearch → "pinning Search"；Foo_bar2 → "Foo bar 2"；pinning_search →
# "pinning search"。unicode61 后续小写化，故大小写无需在此处理。
_SEPARATOR_RUN = re.compile(r"[_\-\s]+")
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")   # fooBar / foo2Bar
_DIGIT_BOUNDARY = re.compile(r"(?<=[a-zA-Z])(?=[0-9])")   # bar2 / Bar2


def split_identifier(text: str) -> str:
    """camel 边界/下划线/数字边界 → 空格（索引与查询双侧共用，保证 MATCH 语义一致）。
    首尾空白 strip（"__init__" → "init" 而非 " init "，与设计例一致；unicode61
    亦把空白当分隔符，strip 不影响索引 token）。"""
    if not text:
        return text
    s = _SEPARATOR_RUN.sub(" ", text)
    s = _CAMEL_BOUNDARY.sub(" ", s)
    s = _DIGIT_BOUNDARY.sub(" ", s)
    return s.strip()


# ── 节点分类（生产口径）───────────────────────────────────────────────────────

def _is_file_node(node: dict) -> bool:
    """file 节点判定：显式 kind='file'（codegraph 适配器路径）或 label 命中
    source_file 的文件名（graphify 原生 AST 路径——文件节点 label == basename）。"""
    if node.get("kind") == "file":
        return True
    label = node.get("label") or ""
    source_file = node.get("source_file") or ""
    return bool(label and source_file and _is_file_node_label(label, source_file))


def _node_kind(node: dict) -> str | None:
    """元数据表 kind 列：显式 kind 属性优先；文件节点归并 'file'；其余如实透传
    （新原生管线 AST 符号不产 kind，此处不臆造——06 票消费侧自行决定语义）。"""
    k = node.get("kind")
    if k == "file":
        return "file"
    if _is_file_node(node):
        return "file"
    return k


def _node_language(node: dict) -> str:
    """元数据表 language 列：language 字段（codegraph 路径）> file_type（原生路径）>
    空串。不做后缀臆测——值域保持与事实层同源。"""
    return node.get("language") or node.get("file_type") or ""


def _metadata_row(node: dict) -> tuple:
    """nodes 元数据表一行（全量节点含 file/语义）——服务点查类工具与过滤路径。
    列序 = _SCHEMA_SQL[0] 表定义序。name/qualified_name 存原始文本（get_node /
    pinning 等点查用）；FTS 拆段文本由 _build 单独入 nodes_fts。"""
    return (
        node.get("id", ""),
        _node_kind(node),
        node.get("label") or "",
        node.get("qualified_name"),
        node.get("signature"),
        node.get("docstring"),
        node.get("source_file"),
        node.get("source_location"),
        node.get("end_line"),
        node.get("end_byte"),
        _node_language(node),
    )


def _fts_row(node: dict) -> tuple | None:
    """nodes_fts 索引行（id/name/qualified_name/docstring/signature，name/qn 为原始
    文本——_build 入库时再拆段）。

    - AST 符号：name=label，qualified_name/docstring/signature 如实取（缺省空串）
    - 语义概念：name=label、qualified_name=label 双列同值（bm25 权重 3+2=5 加成——
      概念 label 本身是高信号文本，被搜到时排前）；docstring/signature 空串起步
      （实测语义节点无 description 字段，接口保留待未来 description 接入）
    - file 节点：不进 FTS（文件名搜索走 pinning / 元数据表 WHERE source_file
      LIKE——进 FTS 只会引入噪声）

    返回 None = 不进 FTS。语义/AST 判定顺序：先 file（file 节点 source_location
    'L1' 也过 _is_ast_tier 的 ^L\\d，必须先短路），再 _is_ast_tier。
    """
    if _is_file_node(node):
        return None
    label = node.get("label") or ""
    if _is_ast_tier(node):
        return (node.get("id", ""), label,
                node.get("qualified_name") or "",
                node.get("docstring") or "",
                node.get("signature") or "")
    return (node.get("id", ""), label, label, "", "")


# ── 构建 ──────────────────────────────────────────────────────────────────────

_SCHEMA_SQL = [
    # nodes 元数据表：全量节点（含 file/语义）。隐式 rowid 供 FTS 行对齐（JOIN）。
    # 列序即 _metadata_row 元组序。
    "CREATE TABLE nodes (id TEXT, kind TEXT, name TEXT, qualified_name TEXT, "
    "signature TEXT, docstring TEXT, source_file TEXT, source_location TEXT, "
    "end_line INTEGER, end_byte INTEGER, language TEXT)",
    # nodes_fts：常规独立 FTS5 表（非外部内容表，见模块 docstring 的核心设计判断），
    # 5 列逐字对齐 codegraph / ranked.py；name/qualified_name 存 camel 拆段文本。
    # 索引随 INSERT/UPDATE 自动同步；'rebuild' 命令亦可用（rebuild_fts_index）。
    "CREATE VIRTUAL TABLE nodes_fts USING fts5("
    "id, name, qualified_name, docstring, signature)",
    "CREATE TABLE meta (mtime_ns INTEGER, size INTEGER, built_at INTEGER)",
]


def _build(graph_path: Path, db_path: Path) -> None:
    """把 graph.json 投影进 db_path（新文件）。分类/行生成见 _fts_row/_metadata_row。

    索引侧文本预拆：name/qualified_name 列经 split_identifier 拆段后入 nodes_fts；
    docstring/signature 列是自然文本，不拆——与查询侧 split_identifier 只在
    identifier token 上应用同构。nodes（原始）与 nodes_fts（拆段）按 rowid 对齐
    （先插 nodes 取 last_insert_rowid，再插 nodes_fts 同 rowid）——过滤路径 JOIN 与
    结果原始名回填都依赖此不变量。

    A1（Task 05 二轮评审）：stat 先于 read_text——构建期间 graph.json 被原子替换时，
    指纹记旧值、内容读新值 → 指纹失配自愈（原实现 read 先 stat：记新指纹配旧内容，
    is_fresh 永真、陈旧内容不自愈）。
    """
    st = graph_path.stat()
    g = json.loads(graph_path.read_text(encoding="utf-8"))
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        conn.execute("BEGIN")
        for sql in _SCHEMA_SQL:
            conn.execute(sql)
        for node in g.get("nodes", []):
            if not isinstance(node, dict):
                continue
            conn.execute(
                "INSERT INTO nodes VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                _metadata_row(node))
            fts = _fts_row(node)
            if fts is None:
                continue
            rowid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            name, qn = split_identifier(fts[1]), split_identifier(fts[2])
            conn.execute(
                "INSERT INTO nodes_fts(rowid, id, name, qualified_name, "
                "docstring, signature) VALUES (?,?,?,?,?,?)",
                (rowid, fts[0], name, qn, fts[3], fts[4]))
        conn.execute("INSERT INTO meta VALUES (?,?,?)",
                     (st.st_mtime_ns, st.st_size, int(time.time())))
        conn.commit()
    finally:
        conn.close()


def rebuild_fts_index(conn: sqlite3.Connection) -> None:
    """FTS 索引同步接口（05 设计"提前留口"）：nodes_fts 行变更后重建 FTS 索引。

    常规 FTS5 表索引本就随 INSERT/UPDATE 自动同步（04 票 watcher 局部更新无需
    手动调本函数）；'rebuild' 命令对常规表同样有效（实测 OK），保留本接口供强制
    重投影/tokenizer 变更场景使用——05 设计"外部内容表同步接口"的语义平移。
    """
    conn.execute("INSERT INTO nodes_fts(nodes_fts) VALUES('rebuild')")


def rebuild_fts(graph_path: str | Path, out_db: str | Path) -> None:
    """从 graph.json 原子构建缓存（tmp 同目录 + os.replace，失败不留下半成品）。

    out_db 最终为 graphify-out/.fts-index.db（隐藏文件）。构建中途任何异常 →
    tmp 清理 + 原缓存不动（若已存在），原子替换保证缓存永远完整可用。
    graph.json 缺失/损坏 → json 读取异常向上传播（事实层有问题，缓存无从建）。
    """
    graph_path = Path(graph_path)
    out_db = Path(out_db)
    out_db.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=out_db.name + ".", suffix=".tmp",
                               dir=str(out_db.parent))
    os.close(fd)
    try:
        _build(graph_path, Path(tmp))
        os.replace(tmp, out_db)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


# ── 指纹与惰性重建 ────────────────────────────────────────────────────────────

def fingerprint(graph_path: str | Path) -> tuple[int, int] | None:
    """graph.json (mtime_ns, size) 指纹；文件缺失 → None（旧链路 db_fingerprint
    的 graph.json 等价物；02 票 freshness 指纹平移语义）。"""
    try:
        st = Path(graph_path).stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def is_fresh(out_db: str | Path, graph_path: str | Path) -> bool:
    """指纹命中 → 缓存可复用。任一不满足（缓存缺失/损坏/schema 版本不符/指纹不
    匹配/图缺失）→ False（触发重建）。读失败（文件被锁/损坏）诚实 False，不崩。"""
    fp = fingerprint(graph_path)
    if fp is None:
        return False
    try:
        conn = sqlite3.connect(f"file:{Path(out_db).resolve().as_posix()}?mode=ro",
                               uri=True)
        try:
            if conn.execute("PRAGMA user_version").fetchone()[0] != _SCHEMA_VERSION:
                return False
            row = conn.execute("SELECT mtime_ns, size FROM meta").fetchone()
            return row is not None and (row[0], row[1]) == fp
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def ensure_fts(graph_path: str | Path, out_db: str | Path) -> bool:
    """惰性重建（serve 首次查询/启动触发）：指纹命中不重建（返回 False）；
    命中失败重建（返回 True）。graph.json 缺失时 rebuild 抛 FileNotFoundError——
    诚实暴露（事实层都没了，缓存无从建），不静默。"""
    if is_fresh(out_db, graph_path):
        return False
    rebuild_fts(graph_path, out_db)
    return True


# ── 只读连接与查询面（消费侧接口：06/10 票）────────────────────────────────────

def open_readonly(db_path: str | Path) -> sqlite3.Connection:
    """URI 只读连接（ranked.py 直查 codegraph.db 时用 adapter._open_readonly；
    缓存链路下 adapter 退役，同款连接函数下沉到本模块）。"""
    p = Path(db_path).resolve()
    return sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)


def _match_expr(identifiers: list[str]) -> str:
    """查询侧预拆：每个 identifier token 经 split_identifier 拆段后，逐段引号包裹
    （防 FTS 语法注入）+ 隐式 AND——与旧链路 ranked.py _fts_search 的逐 token 引号
    + AND 语义对齐，叠加 camelCase 双向预拆（拆段后 snake/camel 符号互相命中）。"""
    terms: list[str] = []
    for tok in identifiers:
        for piece in split_identifier(tok).split():
            if piece:
                terms.append(f'"{piece}"')
    return " ".join(terms)


def fts_search(conn: sqlite3.Connection, identifiers: list[str],
               limit: int = _FTS_LIMIT) -> tuple[list[tuple], int]:
    """FTS5 BM25 通道——ranked.py _fts_search 逐字平移（同 5 列、同列权重、同
    snippet 列 3、同 MATCH 隐式 AND、同 LIMIT 40），加查询侧 camel 预拆。

    返回 (rows, hits)，rows = (id, name, qualified_name, snip, score)——name/qn
    回填 nodes 元数据表原始文本（FTS 表存拆段文本，展示用原始）。MATCH 条件与
    snippet/bm25 必须引用表名 nodes_fts（FTS5 辅助函数不支持别名，实测）。行序
    与 ranked.py _fts_search 一致（pinned 通道消费同款形态）。
    """
    if not identifiers:
        return [], 0
    try:
        match = _match_expr(identifiers)
        if not match:
            return [], 0
        rows = conn.execute(
            f"SELECT f.id, n.name AS name, n.qualified_name AS qualified_name, "
            f"{_SNIPPET_SQL} AS snip, {_BM25_SQL} AS score "
            "FROM nodes_fts AS f JOIN nodes AS n ON n.rowid = f.rowid "
            "WHERE nodes_fts MATCH ? ORDER BY score LIMIT ?",
            (match, limit)).fetchall()
        return rows, len(rows)
    except sqlite3.OperationalError:
        return [], 0


def filtered_search(conn: sqlite3.Connection, match: str, *, kind: str | None = None,
                    source_file: str | None = None,
                    limit: int = _FTS_LIMIT) -> list[tuple]:
    """过滤路径：元数据表 WHERE 先收窄（kind/source_file）再 JOIN FTS5 全文匹配。

    05 设计"过滤路径照搬旧链路"——codegraph 标准路径即元数据表先过滤再 JOIN FTS。
    返回行同 fts_search（id, name, qualified_name, snip, score）。
    match 为已拼好的 MATCH 表达式（见 _match_expr）；kind/source_file 任一为 None
    则不过滤该维度。"""
    sql = (f"SELECT f.id, n.name AS name, n.qualified_name AS qualified_name, "
           f"{_SNIPPET_SQL} AS snip, {_BM25_SQL} AS score "
           "FROM nodes_fts AS f JOIN nodes AS n ON n.rowid = f.rowid "
           "WHERE nodes_fts MATCH ?")
    params: list = [match]
    if kind is not None:
        sql += " AND n.kind = ?"
        params.append(kind)
    if source_file is not None:
        sql += " AND n.source_file = ?"
        params.append(source_file)
    sql += " ORDER BY score LIMIT ?"
    params.append(limit)
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []
