"""serve-memory 票 01：R-E evict-before-reload——重载前先释放旧图，峰值砍约一个图。

spec: docs/specs/serve-memory-spec.md §R-E（:45-53）与 §Testing 验收 1-4（:96-102）。

验收覆盖：
- 验收 1（峰值）：load() 两次驱动（构造 key 变化），tracemalloc 断言重载峰值较
  双图共存基线砍 ≥ 一个图——相对断言（非绝对 50MB）
- 验收 2（逐出红线）：重载刷新不触发 on_evict（缓存回调计数 + watcher 仍 alive）
- 验收 3（并发）：重载窗口内并发 /query 阻塞 ≤2-4s 后正常返回，无 404/500 或空结果
  ——阻塞是预期行为（spec"阻塞窗口"条款），禁止为制造 None 可见窗口把置空移出锁外
- 验收 4（失败）：corrupt graph.json 当次恢复旧图可服务；cache pop 无 None-hit；下查自愈

实现红线（双引用点）：
- cache 侧 load()：key 失配且 entry 存在 → 先 entry["G"]=None / entry["communities"]=None
  再 _load_entry()，完成后整体替换。不 pop、不触发 on_evict、保持 LRU 位（刷新≠容量逐出）。
- 闭包侧 _select_graph：在 _load_ctx 前 G, communities = None, {}；失败恢复旧图
  （except: G, communities = old; raise）。
- 失败路径裁决：cache 侧重载失败 → pop entry（防 G=None+旧 key 命中返回 (None,None) 崩溃）。
"""
import gc
import json
import threading
from pathlib import Path

import pytest
import networkx as nx
from networkx.readwrite import json_graph

from graphify.serve import _GraphContextCache


# === 测试辅助 ===

def _write_graph(path: Path, nodes: list[str]) -> None:
    """写一个最小 graph.json（给定节点 ID 列表），尺寸随节点数变化 → 缓存 key 变化。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    G = nx.DiGraph()
    for n in nodes:
        G.add_node(n, label=n, community=0)
    data = json_graph.node_link_data(G, edges="links")
    path.write_text(json.dumps(data), encoding="utf-8")


def _write_big_graph(path: Path, n: int) -> None:
    """写一个有 n 个节点的图（label/source_file/source_location/community + 边），
    足够大以便 tracemalloc / 重载窗口可测。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    G = nx.Graph()
    for i in range(n):
        G.add_node(f"n{i:05d}", label=f"symbol_{i:05d}",
                   source_file=f"src/mod_{i % 40:02d}/file_{i // 40:03d}.py",
                   source_location=f"L{10 + (i % 300)}", community=i % 5)
    for i in range(1, n):
        G.add_edge(f"n{i:05d}", f"n{(i - 1):05d}",
                   relation="references" if i % 2 else "imports",
                   confidence="EXTRACTED", context="import")
    data = json_graph.node_link_data(G, edges="links")
    path.write_text(json.dumps(data), encoding="utf-8")


def _mini_proj(tmp_path: Path) -> Path:
    """mini 项目（2 个 Python 源文件）；标准布局 <root>/graphify-out/graph.json。"""
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "a.py").write_text(
        "import b\n\ndef foo():\n    return b.bar()\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("def bar():\n    return 1\n", encoding="utf-8")
    return tmp_path


def _graph_cell(select_fn):
    """从 _select_graph 闭包中定位持有 nx.Graph 的 cell（white-box 读非局部 G）。

    前提：最近一次成功加载后 G 是非 None 的 nx.Graph——可据此唯一定位 G cell
    （communities 是 dict、active_graph_path 是 str、_registry 是 None 或
    WatcherRegistry，均非 nx.Graph）。
    """
    for cell in select_fn.__closure__ or ():
        if isinstance(cell.cell_contents, nx.Graph):
            return cell
    raise AssertionError("_select_graph 闭包中未找到持有 nx.Graph 的 cell")


# === 验收 1：R-E 峰值（tracemalloc 相对断言）====

def _measure_reload(graph_path: str, hold_old: bool, reload_n: int) -> tuple[int, int]:
    """tracemalloc 测一次重载的稳态内存与峰值（current, peak）。

    hold_old=True：外部强引用旧图 → 模拟双图共存基线（旧图在重载期间不释放）。
    hold_old=False：evict-before-reload（entry["G"]=None 释放旧图后再加载新图）。
    调用方预置 warm 版文件；本函数写 reload_n 版触发 key 变化。

    稳态 current 是基线完整性校验：基线（hold_old）重载后仍持旧图=双图，修复=单图。
    峰值 peak 是 spec 口径（"重载峰值较双图共存基线砍 ≥ 一个图"）与 RED/GREEN 判别
    主口径，但 json 解析峰值（基线/修复共有）叠加分配器噪声，故阈值放宽为相对 0.85。
    """
    import tracemalloc
    cache = _GraphContextCache(8)
    held = None
    gc.collect()
    tracemalloc.start()
    cache.load(graph_path)                                # warm 加载（分配旧图）
    if hold_old:
        held = cache._entries[graph_path]["G"]            # 强引用旧图 → 双图共存
    _write_big_graph(Path(graph_path), reload_n)          # 变更文件 → key 变化
    cache.load(graph_path)                                # 重载
    cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return cur, peak


def test_reload_peak_cuts_old_graph(tmp_path):
    """验收 1：重载后旧图不再叠加——相对断言，非绝对 50MB。

    直接驱动 _GraphContextCache.load() 两次（构造 key 变化）。双图共存基线 =
    外部强引用旧图使其在重载期间存活；evict-before-reload = 缓存先置空再加载。
    修复前两者稳态/峰值相近（旧图被缓存 entry 持有），修复后仅新图驻留。
    """
    graph_path = str(tmp_path / "graph.json")
    _write_big_graph(Path(graph_path), 3000)
    cur_baseline, peak_baseline = _measure_reload(graph_path, hold_old=True, reload_n=3500)
    _write_big_graph(Path(graph_path), 3000)
    cur_fix, peak_fix = _measure_reload(graph_path, hold_old=False, reload_n=3500)
    # 基线完整性：hold_old 基线重载后确持双图（cur_baseline 显著大于 cur_fix）。
    # 注意：该断言非 RED/GREEN 判别（重载完成后旧 entry 被替换、旧图无论修复与否
    # 都会释放），只校验"双图共存基线"构造真实有效。
    assert cur_fix < cur_baseline * 0.8, (
        f"基线构造异常：cur_fix={cur_fix} 应显著小于 cur_baseline={cur_baseline}"
    )
    # 峰值（spec 口径 + RED/GREEN 判别主口径）：重载峰值较双图共存基线砍 ≥ 一个图
    # （相对断言）。修复前旧图在重载期间被缓存 entry 持有 → 峰值≈基线；修复后先释放
    # 旧图再加载 → 峰值显著下降。
    assert peak_fix < peak_baseline * 0.85, (
        f"重载峰值未砍掉旧图：peak_fix={peak_fix} 应显著小于 "
        f"peak_baseline={peak_baseline}（双图共存基线）"
    )


def test_reload_frees_old_graph_before_new_loads(tmp_path):
    """验收 1（机制口径，确定性补强）：重载先释放旧图再加载新图——"旧图新图不同时
    驻留"的直接验证。tracemalloc 峰值受分配器噪声影响（解析峰值叠加），此断言在
    _load_entry 进行中探针旧图 weakref：修复前旧图在加载新图期间仍存活（双图共存），
    修复后 entry["G"]=None 已先行释放旧图。
    """
    import weakref
    cache = _GraphContextCache(8)
    graph_path = str(tmp_path / "graph.json")
    _write_big_graph(Path(graph_path), 2000)
    cache.load(graph_path)
    old_ref = weakref.ref(cache._entries[graph_path]["G"])
    assert old_ref() is not None, "前置：旧图对象存活"
    observed: dict[str, bool] = {}
    _orig_load_entry = cache._load_entry

    def _load_entry_with_probe(path, key):
        observed["old_alive_during_load"] = old_ref() is not None
        return _orig_load_entry(path, key)

    cache._load_entry = _load_entry_with_probe
    _write_big_graph(Path(graph_path), 2500)              # key 变化
    cache.load(graph_path)                                # 重载
    assert observed.get("old_alive_during_load") is False, \
        "加载新图时旧图仍存活（未先释放）——旧图+新图双驻留"


# === 验收 2：逐出红线（重载刷新不触发 on_evict）====

def test_reload_does_not_trigger_on_evict_callback(tmp_path):
    """验收 2（缓存级）：重载刷新不触发 on_evict 回调——on_evict 仅容量 popitem 触发
    （刷新≠容量逐出）；容量逐出仍应触发（正向对照）。"""
    evicted = []
    cache = _GraphContextCache(max_contexts=8, on_evict=lambda k: evicted.append(k))
    graph_path = str(tmp_path / "graph.json")
    _write_graph(Path(graph_path), ["alpha"])
    cache.load(graph_path)
    _write_graph(Path(graph_path), ["alpha", "beta"])     # key 变化
    cache.load(graph_path)                                # 重载刷新
    assert evicted == [], f"重载刷新不应触发 on_evict，got {evicted}"
    # 正向对照：容量逐出仍触发
    evicted.clear()
    cache2 = _GraphContextCache(max_contexts=2, on_evict=lambda k: evicted.append(k))
    for i in range(4):
        p = str(tmp_path / f"p{i}" / "graph.json")
        _write_graph(Path(p), ["alpha"])
        cache2.load(p)
    assert len(evicted) == 2, f"容量逐出应触发 on_evict 两次，got {evicted}"


@pytest.fixture
def polling(monkeypatch):
    """强制降级轮询（无 watchdog 语义，确定性测试 watcher）。"""
    import graphify.serve_watcher as W
    monkeypatch.setattr(W, "_WatchdogObserver", None)
    monkeypatch.setattr(W, "_FSHandler", None)


@pytest.fixture
def fast_watch(monkeypatch):
    """serve 构建前注入短防抖/快轮询（registry 构造时读模块常量）。"""
    import graphify.serve_watcher as W
    monkeypatch.setattr(W, "DEFAULT_DEBOUNCE", 0.1)
    monkeypatch.setattr(W, "DEFAULT_POLL_INTERVAL", 0.2)


def test_reload_refresh_does_not_evict_watcher(polling, fast_watch, tmp_path):
    """验收 2（watcher 面）：重载刷新不触发 on_evict——默认项目 watcher 仍 alive。
    on_evict 由容量逐出接线到 evict_graph（停对应 watcher），重载刷新不得停 watcher。"""
    import graphify.serve as S
    root = _mini_proj(tmp_path)
    graph_path = root / "graphify-out" / "graph.json"
    graph_path.parent.mkdir(parents=True, exist_ok=True)
    _write_graph(graph_path, ["alpha", "beta"])
    server = S._build_server(str(graph_path), watch=True)
    registry = getattr(server, "_graphify_registry", None)
    assert registry is not None
    try:
        w = registry.get(root)
        assert w is not None and w.is_alive, "前置：默认项目 watcher 应已挂载且 alive"
        # 变更 graph.json → key 变化 → 重载刷新
        _write_graph(graph_path, ["alpha", "beta", "gamma"])
        server._graphify_select_graph(str(root))
        assert w.is_alive, "重载刷新不应停 watcher（on_evict 仅容量逐出触发）"
        # 重载确实发生且缓存持新图（默认图 pinned → 经 cache.get 同时查 _pinned/_entries）
        resolved = str(Path(graph_path).resolve())
        entry = registry._ctx_cache.get(resolved)
        assert entry is not None and "gamma" in entry["G"].nodes(), "重载后缓存应持新图"
    finally:
        registry.stop_all()


# === 验收 3：并发 /query 在重载窗口内阻塞后正常返回 ===

def test_query_during_reload_returns_normally_after_reload_window(tmp_path):
    """验收 3：重载窗口内到达的 /query 全部正常返回，无 404/500 或空结果。

    局限说明（不谎称真并发）：starlette TestClient 经 anyio portal 把 5 个线程的 /query
    串行化到单一事件循环——首个查询触发重载（持缓存锁 + 锁内 json 解析，数百 ms），
    其余查询在事件循环/传输层排队（阻塞窗口，spec 预期行为非降级），重载完成后全部
    返回。本测试验证"重载窗口内到达的查询都正常返回"，不强于实际激发行为。
    """
    pytest.importorskip("mcp")
    pytest.importorskip("starlette")
    import graphify.serve as S
    from starlette.testclient import TestClient

    root = _mini_proj(tmp_path)
    graph_path = root / "graphify-out" / "graph.json"
    graph_path.parent.mkdir(parents=True, exist_ok=True)
    _write_big_graph(graph_path, 4000)
    app = S._build_http_app(str(graph_path), json_response=True)
    results: dict[int, object] = {}

    def _query(i: int):
        # 共享同一 TestClient（starlette TestClient 经 anyio portal 串行化并发请求；
        # 每个线程各自 with TestClient 会重复进入 app lifespan → manager.run() 只能一次）
        results[i] = client.post(
            "/query", json={"prompt": "symbol_00001", "graph_path": str(root)})

    with TestClient(app, base_url="http://127.0.0.1") as client:
        # warm：首次加载（缓存 miss → load）
        r = client.post("/query", json={"prompt": "symbol_00001", "graph_path": str(root)})
        assert r.status_code == 200, r.text
        # 变更 graph.json → 下一次查询触发重载（大图 → 重载窗口可观）
        _write_big_graph(graph_path, 4500)
        threads = [threading.Thread(target=_query, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
    for i, resp in sorted(results.items()):
        assert resp.status_code == 200, f"并发 /query #{i} 返回 {resp.status_code}: {resp.text}"
        body = resp.json()
        assert "result" in body and body["result"], f"并发 /query #{i} 空结果/异常：{body}"


# === 验收 4：corrupt graph.json——pop 无 None-hit；恢复旧图；下查自愈 ===

def test_reload_failure_pops_cache_no_none_hit_and_self_heals(tmp_path):
    """验收 4（缓存级）：重载失败（corrupt graph.json）→ pop entry，无 None-hit
    （get 返回 None 而非 (None, None) 崩溃路径）；修复后下查自愈。"""
    cache = _GraphContextCache(8)
    graph_path = str(tmp_path / "graph.json")
    _write_graph(Path(graph_path), ["alpha", "beta"])
    G1, _comm1 = cache.load(graph_path)
    assert set(G1.nodes()) == {"alpha", "beta"}
    assert cache.get(graph_path) is not None
    # 变更 key 后破坏 graph.json → 重载失败
    Path(graph_path).write_text("{not valid json", encoding="utf-8")
    with pytest.raises(RuntimeError):
        cache.load(graph_path)
    # 失败裁决：pop entry → get 返回 None（缓存只持有确认新鲜的图）
    assert cache.get(graph_path) is None, "重载失败后旧 entry 应被 pop（无 None-hit）"
    # 下查自愈：修复 graph.json → 重载成功，返回新图
    _write_graph(Path(graph_path), ["alpha", "beta", "gamma"])
    G2, _comm2 = cache.load(graph_path)
    assert "gamma" in G2.nodes()
    assert cache.get(graph_path) is not None


def test_select_graph_leaves_G_none_on_failure_and_self_heals(tmp_path):
    """验收 4（闭包侧，I1 修订）：_select_graph 重载失败 G 停留 None——G 消费者全部在
    _select_graph 成功后读闭包 G（失败即 500/isError），无路径读 None 中间态；修复后
    下查自愈。

    white-box：经 __closure__ 定位 G cell（_graph_cell 按 nx.Graph 类型识别）。G cell
    引用在失败前取得（此时持 nx.Graph），失败后同一 cell 应为 None。
    原"失败恢复旧图"已按 I1 删除——old 恢复强引用旧图会抵消闭包路径峰值削减。
    """
    import graphify.serve as S
    root = _mini_proj(tmp_path)
    graph_path = root / "graphify-out" / "graph.json"
    graph_path.parent.mkdir(parents=True, exist_ok=True)
    _write_graph(graph_path, ["alpha", "beta"])
    server = S._build_server(str(graph_path))            # 不开 watcher（无需 registry）
    server._graphify_select_graph(str(root))             # 成功加载 → G = 旧图
    gcell = _graph_cell(server._graphify_select_graph)
    old_g = gcell.cell_contents
    assert old_g is not None and "alpha" in old_g.nodes()
    # 破坏 graph.json → 重载失败 → 闭包 G 停留 None（不恢复旧图）
    Path(graph_path).write_text("{not valid json", encoding="utf-8")
    with pytest.raises(RuntimeError):
        server._graphify_select_graph(str(root))
    assert gcell.cell_contents is None, "重载失败后闭包 G 应停留 None（I1：不恢复旧图）"
    # 修复 → 下查自愈
    _write_graph(graph_path, ["alpha", "beta", "gamma"])
    server._graphify_select_graph(str(root))
    assert "gamma" in gcell.cell_contents.nodes(), "修复后下查应自愈（闭包 G 更新为新图）"


def test_select_graph_reload_frees_old_graph(polling, fast_watch, tmp_path):
    """I1 回归网（本应抓到 I1 的测试）：闭包路径重载必须真实释放旧图——经 _select_graph
    驱动重载，_load_entry 进行中旧图 weakref 已死。闭包 G 与 cache entry["G"] 同一对象，
    双释放缺一不可；曾经的 old 恢复会强引用旧图使本探针抓到旧图存活（生产峰值削减被抵消）。"""
    import weakref
    import graphify.serve as S
    root = _mini_proj(tmp_path)
    graph_path = root / "graphify-out" / "graph.json"
    graph_path.parent.mkdir(parents=True, exist_ok=True)
    _write_big_graph(graph_path, 2000)
    server = S._build_server(str(graph_path), watch=True)
    registry = server._graphify_registry
    try:
        server._graphify_select_graph(str(root))        # 加载旧图（闭包路径）
        gcell = _graph_cell(server._graphify_select_graph)
        old_ref = weakref.ref(gcell.cell_contents)
        assert old_ref() is not None, "前置：旧图对象存活"
        observed: dict[str, bool] = {}
        _orig = registry._ctx_cache._load_entry

        def _probe(path, key):
            observed["old_alive_during_load"] = old_ref() is not None
            return _orig(path, key)

        registry._ctx_cache._load_entry = _probe
        _write_big_graph(graph_path, 2500)              # key 变化
        server._graphify_select_graph(str(root))        # 闭包路径重载
        assert observed.get("old_alive_during_load") is False, \
            "闭包路径重载期间旧图仍存活（I1：old 恢复强引用抵消峰值削减）"
    finally:
        registry.stop_all()


def test_cache_lock_blocks_get_during_reload_never_nulled(tmp_path):
    """M1：锁内红线回归网——重载（load 持锁：置空→加载→替换原子）期间，并发 get()
    阻塞至重载完成，返回重载后有效 entry（永不见 G=None 中间态）。若把置空移出锁外，
    并发 get() 会在空窗内直接拿到 nulled entry——本探针抓此回归。"""
    import threading
    import time as _time
    from graphify.serve import _GraphContextCache
    cache = _GraphContextCache(8)
    graph_path = str(tmp_path / "graph.json")
    _write_big_graph(Path(graph_path), 2000)
    cache.load(graph_path)
    _write_big_graph(Path(graph_path), 2500)            # key 变化 → 重载
    entered = threading.Event()
    proceed = threading.Event()
    get_started = threading.Event()
    _orig = cache._load_entry

    def _stall_load_entry(path, key):
        entered.set()
        proceed.wait(timeout=30)                        # 卡住重载 → get() 必在重载窗口内发起
        return _orig(path, key)

    cache._load_entry = _stall_load_entry
    load_results = []

    def _do_load():
        try:
            cache.load(graph_path)
            load_results.append("ok")
        except Exception as e:  # pragma: no cover
            load_results.append(f"err:{e}")

    lt = threading.Thread(target=_do_load)
    lt.start()
    assert entered.wait(timeout=10), "重载未进入 _load_entry（卡点失效）"
    get_results = []

    def _do_get():
        get_started.set()
        t0 = _time.perf_counter()
        entry = cache.get(graph_path)
        get_results.append((_time.perf_counter() - t0, entry))

    gt = threading.Thread(target=_do_get)
    gt.start()
    get_started.wait(timeout=10)
    _time.sleep(0.2)                                    # 让 get() 进入锁阻塞
    proceed.set()                                       # 放行重载
    lt.join(timeout=30)
    gt.join(timeout=30)
    assert load_results == ["ok"], f"重载失败：{load_results}"
    elapsed, entry = get_results[0]
    assert entry is not None and entry["G"] is not None, \
        f"并发 get() 不应见 nulled entry，got {entry}"
    assert elapsed >= 0.2, f"get() 应阻塞至重载完成（锁互斥），elapsed={elapsed:.3f}s"


def test_query_during_corrupt_graph_errors_then_self_heals(tmp_path):
    """验收 4（HTTP 面）：corrupt graph.json 期间每查报错（500，与今日行为一致——
    今日 key 恒失配同样从不服务旧图）；修复后下查自愈（200 非空）。"""
    pytest.importorskip("mcp")
    pytest.importorskip("starlette")
    import graphify.serve as S
    from starlette.testclient import TestClient
    root = _mini_proj(tmp_path)
    graph_path = root / "graphify-out" / "graph.json"
    graph_path.parent.mkdir(parents=True, exist_ok=True)
    _write_graph(graph_path, ["alpha", "beta"])
    app = S._build_http_app(str(graph_path), json_response=True)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        r = client.post("/query", json={"prompt": "alpha", "graph_path": str(root)})
        assert r.status_code == 200, r.text
        # 破坏图 → 每查报错（重载失败走 handler except → 500）
        Path(graph_path).write_text("{corrupt json", encoding="utf-8")
        r = client.post("/query", json={"prompt": "alpha", "graph_path": str(root)})
        assert r.status_code == 500, f"corrupt 期间每查应报错，got {r.status_code}: {r.text}"
        assert "graph load failed" in r.json().get("error", "")
        # 修复 → 下查自愈
        _write_graph(graph_path, ["alpha", "beta", "gamma"])
        r = client.post("/query", json={"prompt": "alpha", "graph_path": str(root)})
        assert r.status_code == 200, r.text
        assert r.json()["result"], "修复后下查应自愈并返回非空结果"
