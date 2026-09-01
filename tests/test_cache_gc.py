"""B4 GC：live 全存活 / orphan 宽限窗后 sweep / 宽限窗内保留 / 旧版本目录清理 / 频率门控."""
import json, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import cache_gc
from cache_gc import gc_cache, _sweep

# fixture 用 vCUR/vOLD 占位版本目录名——_sweep 的"旧版本目录整删"分支按 _CACHE_VERSION
# 判别当前版本，测试里覆盖为 vCUR 使语义闭合（生产值 = graphify 实际版本目录名）。
cache_gc._CACHE_VERSION = "vCUR"

_NOW = 1_800_000_000.0

def _setup(tmp_path):
    out = tmp_path / "graphify-out"
    for ver in ("vCUR", "vOLD"):
        (out / "cache" / "ast" / ver).mkdir(parents=True)
    (out / "cache" / "semantic").mkdir(parents=True)
    for h in ("live1", "orphan", "grace"):
        (out / "cache" / "ast" / "vCUR" / f"{h}.json").write_text("x" * 100)
        (out / "cache" / "semantic" / h).write_text("x" * 100)
    (out / "cache" / "ast" / "vOLD" / "stale.json").write_text("x" * 100)
    old, recent = _NOW - 8 * 86400, _NOW - 86400   # 8 天前 / 1 天前（7 天宽限窗两侧）
    for h, mt in (("live1", recent), ("orphan", old), ("grace", recent)):
        os.utime(out / "cache" / "ast" / "vCUR" / f"{h}.json", (mt, mt))
        os.utime(out / "cache" / "semantic" / h, (mt, mt))
    os.utime(out / "cache" / "ast" / "vOLD" / "stale.json", (old, old))
    (out / "manifest.json").write_text(json.dumps(
        {"a.py": {"mtime": 0, "ast_hash": "live1", "semantic_hash": "live1"}}), encoding="utf-8")
    return out

def test_sweep_logic_orphans_vs_live(tmp_path):
    """sweep 逻辑与门控解耦直测（E3）：live 全存活 / orphan 宽限窗外删 / 宽限窗内保留 / 旧版本目录整删."""
    out = _setup(tmp_path)
    r = _sweep(out, live={"live1"}, now_ts=_NOW)
    cur = out / "cache" / "ast" / "vCUR"
    assert (cur / "live1.json").exists()                      # manifest 锚定 100% 保留
    assert not (cur / "orphan.json").exists()                 # 宽限窗外 orphan 被 sweep
    assert (cur / "grace.json").exists()                      # 宽限窗内保留（checkout 回退保护）
    assert not (out / "cache" / "ast" / "vOLD").exists()      # 旧版本目录整删
    assert r["swept"] >= 2 and r["bytes_freed"] > 0           # ast/orphan + semantic/orphan

def test_gc_frequency_gate(tmp_path):
    out = _setup(tmp_path)
    r = gc_cache(out, out / "manifest.json", now_ts=_NOW)
    assert r["triggered"] is False   # 4 条目 vs live×2+64=66 门控未过，sweep 不执行
    assert r["swept"] == 0
