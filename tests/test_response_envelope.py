"""A1b 信封：verdict 三分 / freshness 推导 / 时效逃生 / 尾部行格式 / isError 边界."""
import json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import networkx as nx
import pytest
from graphify.serve import _envelope, _derive_verdict, _derive_freshness

def test_envelope_appends_single_meta_line():
    out = _envelope("Node: foo\n  Degree: 2", verdict="ok", freshness="fresh",
                    scanned_nodes=42)
    assert out.startswith("Node: foo\n  Degree: 2")   # 既有正文逐字节不变
    lines = out.rstrip("\n").split("\n")
    assert lines[-2] == ""                             # 空行分隔
    meta = json.loads(lines[-1].removeprefix("_meta: "))
    assert meta["verdict"] == "ok" and meta["freshness"] == "fresh"
    assert meta["scanned_nodes"] == 42

def test_verdict_absent_carries_scan_count():
    v, meta = _derive_verdict(tool="get_node", found=False, scanned_nodes=1)
    assert v == "absent" and meta["scanned_nodes"] == 1

def test_verdict_absent_empty_graph_flag():
    v, meta = _derive_verdict(tool="query_graph", found=False, scanned_nodes=0)
    assert v == "absent" and meta["empty_graph"] is True

def test_freshness_stale_escape_hatch(tmp_path):
    """kill -9 残留 rebuilding 状态文件 -> 超时效判 stale_index（Q3 裁决）.
    测试手写 last_duration=10.0：N2 后生产写入器继承上轮 complete 的该字段，
    此形态即生产真实形态（rebuild_entry._read_prev_duration 继承链）."""
    state = tmp_path / "graphify-out" / ".rebuild-state.json"
    state.parent.mkdir()
    state.write_text(json.dumps({
        "schema": 1, "phase": "rebuilding", "started": time.time() - 3600,
        "last_duration": 10.0}), encoding="utf-8")   # 超过 max(2*10, 1800)
    assert _derive_freshness(state) == "stale_index"

def test_freshness_rebuilding_within_window(tmp_path):
    state = tmp_path / "graphify-out" / ".rebuild-state.json"
    state.parent.mkdir()
    state.write_text(json.dumps({"schema": 1, "phase": "rebuilding",
                                 "started": time.time() - 5}), encoding="utf-8")
    assert _derive_freshness(state) == "rebuilding"

def test_freshness_no_state_file_is_fresh(tmp_path):
    """守卫回退原则：未迁移项目无状态文件，不得全标 stale."""
    assert _derive_freshness(tmp_path / "nonexistent" / ".rebuild-state.json") == "fresh"

def test_freshness_non_dict_state_file_is_fresh(tmp_path):
    """终审 Imp-1：状态文件是损坏的非 dict（如 []）→ freshness 判 fresh 不崩.
    call_tool 对每个工具（含 list_prs）都求值 freshness，AttributeError 会把所有
    调用变 Error executing、serve 永不自愈；非 dict 即损坏形态守卫回退."""
    state = tmp_path / "graphify-out" / ".rebuild-state.json"
    state.parent.mkdir()
    state.write_text("[]", encoding="utf-8")
    assert _derive_freshness(state) == "fresh"

def test_freshness_missing_graph_json_is_stale(tmp_path):
    """R5-3：complete 态但 graph.json 缺失 -> stale_index 不崩（freshness 是附加层，
    无权杀死响应；产物缺失即最陈旧形态）."""
    state = tmp_path / "graphify-out" / ".rebuild-state.json"
    state.parent.mkdir()
    state.write_text(json.dumps({"schema": 1, "phase": "complete", "last_duration": 5.0}),
                     encoding="utf-8")
    assert _derive_freshness(state) == "stale_index"   # 无 graph.json 文件

def test_verdict_degraded_overrides_found():
    """rebuilding 窗口压倒一切（G1 调用链：出口负责传 degraded=True）."""
    v, meta = _derive_verdict(tool="get_node", found=True, scanned_nodes=1, degraded=True)
    assert v == "degraded"

def test_apply_envelope_dispatches_tuple_and_bare_str():
    """N1 返回契约：检索型解包 (text, found, scanned) 过信封；清单外裸 str 直通.
    R3-4：断言用 json.loads 解析——_envelope 是 compact separators（无空格），子串断言易漂移."""
    import json as _json
    from graphify.serve import _apply_envelope
    # 检索型：三元组解包 + absent 携带扫描计数
    out = _apply_envelope("get_node", ("Node: x", False, 1), freshness="fresh")
    meta = _json.loads(out.rstrip("\n").split("\n")[-1].removeprefix("_meta: "))
    assert meta["verdict"] == "absent" and meta["scanned_nodes"] == 1
    # 非检索型：裸 str 原样直通（list_prs 等不进清单）
    assert _apply_envelope("list_prs", "PR #1 ...", freshness="fresh") == "PR #1 ..."

def test_apply_envelope_verdict_override():
    """R3-3：low_confidence 无 _derive_verdict 推导路径——由工具经 verdict_override 直通
    （B3 blast-radius/fanout 输出、C 系分析工具用；depth=1 普通邻接仍走推导得 ok）."""
    import json as _json
    from graphify.serve import _apply_envelope
    out = _apply_envelope("get_neighbors", ("Neighbors of x:", True, 3),
                          freshness="fresh", verdict_override="low_confidence")
    meta = _json.loads(out.rstrip("\n").split("\n")[-1].removeprefix("_meta: "))
    assert meta["verdict"] == "low_confidence" and meta["freshness"] == "fresh"

def test_apply_envelope_degraded_beats_override():
    """B2 优先级：degraded 压倒一切——freshness=rebuilding 时 override 不得压掉 degraded
    （Task 2 minor 交接：override 分支绕过 degraded 是历史缺陷，B2 是 override 首个真实
    消费者，两者可同现（rebuild 期间 get_node 切片失败）——顺带修正+锁定）."""
    import json as _json
    from graphify.serve import _apply_envelope
    out = _apply_envelope("get_node", ("Node: x", True, 1),
                          freshness="rebuilding", verdict_override="low_confidence")
    meta = _json.loads(out.rstrip("\n").split("\n")[-1].removeprefix("_meta: "))
    assert meta["verdict"] == "degraded" and meta["freshness"] == "rebuilding"
    # override 仍直通（非 degraded 时）——R3-3 不破
    out2 = _apply_envelope("get_node", ("Node: x", True, 1),
                           freshness="fresh", verdict_override="low_confidence")
    meta2 = _json.loads(out2.rstrip("\n").split("\n")[-1].removeprefix("_meta: "))
    assert meta2["verdict"] == "low_confidence" and meta2["freshness"] == "fresh"

def test_apply_envelope_four_tuple_override_flow():
    """B2 N1 扩展：result 四元组 (text, found, scanned, verdict_override) 时取末元直通
    （get_node 工具自报 low_confidence/absent 的通道）；显式 param 优先，契约不破."""
    import json as _json
    from graphify.serve import _apply_envelope
    out = _apply_envelope("get_node", ("Node: x", True, 1, "absent"), freshness="fresh")
    meta = _json.loads(out.rstrip("\n").split("\n")[-1].removeprefix("_meta: "))
    assert meta["verdict"] == "absent"
    # 四元组 + 显式 param：param 优先（R3-3 契约）
    out2 = _apply_envelope("get_node", ("Node: x", True, 1, "absent"),
                           freshness="fresh", verdict_override="low_confidence")
    meta2 = _json.loads(out2.rstrip("\n").split("\n")[-1].removeprefix("_meta: "))
    assert meta2["verdict"] == "low_confidence"
