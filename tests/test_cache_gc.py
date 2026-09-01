"""B4 GC：live 全存活 / orphan 宽限窗后 sweep / 宽限窗内保留 / 旧版本目录清理 / 频率门控 /
生产形态 live=0（锚定失效）TTL 全量轮换。sweep 范围 = cache/ast 当前版本目录（semantic 不 sweep）。"""
import json, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import cache_gc
from cache_gc import gc_cache, _sweep

# fixture 用 vCUR/vOLD 占位版本目录名——_sweep 的"旧版本目录整删"分支按 _CACHE_VERSION
# 判别当前版本，测试里覆盖为 vCUR 使语义闭合（生产值 = graphify 实际版本目录名）。
cache_gc._CACHE_VERSION = "vCUR"

_NOW = 1_800_000_000.0
_OLD = _NOW - 8 * 86400        # 8 天前（7 天宽限窗之外）
_RECENT = _NOW - 86400         # 1 天前（宽限窗内）

def _setup(tmp_path):
    """理想化 fixture：可读 stem 模拟"锚定已恢复"的假想形态（真实生产 cache 文件名是
    64-hex SHA256、manifest hash 是 32-hex MD5，二者结构性不相交——生产形态另测
    test_production_shape_live_empty_ttl_sweep）。"""
    out = tmp_path / "graphify-out"
    for ver in ("vCUR", "vOLD"):
        (out / "cache" / "ast" / ver).mkdir(parents=True)
    for h in ("live1", "orphan", "grace"):
        (out / "cache" / "ast" / "vCUR" / f"{h}.json").write_text("x" * 100)
    (out / "cache" / "ast" / "vOLD" / "stale.json").write_text("x" * 100)
    for h, mt in (("live1", _RECENT), ("orphan", _OLD), ("grace", _RECENT)):
        os.utime(out / "cache" / "ast" / "vCUR" / f"{h}.json", (mt, mt))
    os.utime(out / "cache" / "ast" / "vOLD" / "stale.json", (_OLD, _OLD))
    (out / "manifest.json").write_text(json.dumps(
        {"a.py": {"mtime": 0, "ast_hash": "live1", "semantic_hash": "live1"}}), encoding="utf-8")
    return out

def test_sweep_logic_orphans_vs_live(tmp_path):
    """sweep 逻辑与门控解耦直测（E3，假想锚定已恢复形态）：live 全存活 / orphan 宽限窗外删 /
    宽限窗内保留 / 旧版本目录整删。"""
    out = _setup(tmp_path)
    r = _sweep(out, live={"live1"}, now_ts=_NOW)
    cur = out / "cache" / "ast" / "vCUR"
    assert (cur / "live1.json").exists()                      # live 锚定 100% 保留
    assert not (cur / "orphan.json").exists()                 # 宽限窗外 orphan 被 sweep
    assert (cur / "grace.json").exists()                      # 宽限窗内保留（checkout 回退保护）
    assert not (out / "cache" / "ast" / "vOLD").exists()      # 旧版本目录整删
    assert r["swept"] >= 2 and r["bytes_freed"] > 0           # orphan + vOLD/stale

def test_gc_frequency_gate(tmp_path):
    out = _setup(tmp_path)
    r = gc_cache(out, out / "manifest.json", now_ts=_NOW)
    assert r["triggered"] is False   # 4 ast 条目 vs live×2+64=66 门控未过，sweep 不执行
    assert r["swept"] == 0

def test_production_shape_live_empty_ttl_sweep(tmp_path):
    """生产形态（Important-3）：cache 文件名 64-hex SHA256、manifest hash 32-hex MD5 结构性
    不相交 -> live=0 -> 宽限窗后全部条目被 sweep（7 天 TTL 全量轮换，文档化真实行为）；
    live=0 且条目>64 时门控触发 triggered=True（真实生产行为）。"""
    out = tmp_path / "graphify-out"
    ver = out / "cache" / "ast" / "vCUR"; ver.mkdir(parents=True)
    for i in range(65):                       # > 0*2+64 门控
        f = ver / f"{i:064x}.json"            # 64-hex SHA-256 形态（贴真实生产）
        f.write_text("x" * 10)
        os.utime(f, (_OLD, _OLD))
    (out / "manifest.json").write_text(json.dumps(     # 32-hex MD5 形态——与 cache 文件名不相交
        {"a.py": {"mtime": 0, "ast_hash": f"{1:032x}", "semantic_hash": f"{2:032x}"}}),
        encoding="utf-8")
    r = gc_cache(out, out / "manifest.json", now_ts=_NOW)
    assert r["triggered"] is True             # live=0 且条目>64 -> 门控过
    assert r["live"] == 0                     # 锚定失效，live 恒空
    assert r["swept"] == 65                   # 宽限窗外全部条目被 sweep
    assert not any(ver.iterdir())             # 当前版本目录被清空（TTL 全量轮换）
