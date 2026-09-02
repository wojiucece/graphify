"""B4 GC：live 全存活 / orphan 宽限窗后 sweep / 宽限窗内保留 / 旧版本目录清理 / 频率门控 /
生产形态锚定命中（live 由 file_hash 重算，round 2 用户裁决）/ 文件已删除 manifest 条目安全跳过。"""
import json, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import cache_gc
from cache_gc import gc_cache, _sweep
from graphify.cache import file_hash

# fixture 用 vCUR/vOLD 占位版本目录名——_sweep 的"旧版本目录整删"分支按 _CACHE_VERSION
# 判别当前版本，测试里覆盖为 vCUR 使语义闭合（生产值 = graphify 实际版本目录名）。
cache_gc._CACHE_VERSION = "vCUR"

_NOW = 1_800_000_000.0
_OLD = _NOW - 8 * 86400        # 8 天前（7 天宽限窗之外）
_RECENT = _NOW - 86400         # 1 天前（宽限窗内）

def _setup(tmp_path):
    """理想化 fixture（纯函数直测 live 集合）：可读 stem 假想"锚定已命中"形态；
    生产形态另测 test_production_shape_anchor_hit（真实 file_hash 同域锚定）。"""
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
    """sweep 逻辑与门控解耦直测（E3，假想 live 集合直传）：live 全存活 / orphan 宽限窗外删 /
    宽限窗内保留 / 旧版本目录整删。"""
    out = _setup(tmp_path)
    r = _sweep(out, live={"live1"}, now_ts=_NOW)
    cur = out / "cache" / "ast" / "vCUR"
    assert (cur / "live1.json").exists()                      # live 锚定 100% 保留
    assert not (cur / "orphan.json").exists()                 # 宽限窗外 orphan 被 sweep
    assert (cur / "grace.json").exists()                      # 宽限窗内保留（checkout 回退保护）
    assert not (out / "cache" / "ast" / "vOLD").exists()      # 旧版本目录整删
    assert r["swept"] >= 2 and r["bytes_freed"] > 0           # orphan + vOLD/stale

def test_gc_frequency_gate_live_empty(tmp_path):
    """live=∅（manifest 文件无真实文件对应）时门控未过不 sweep——floor 64 下静默."""
    out = _setup(tmp_path)
    r = gc_cache(out, out / "manifest.json", now_ts=_NOW, root=tmp_path)
    assert r["triggered"] is False   # 4 ast 条目 vs 0*2+64=64 门控未过，sweep 不执行
    assert r["swept"] == 0

def test_production_shape_anchor_hit(tmp_path):
    """生产形态锚定命中（round 2 用户裁决）：真实文件 + manifest（值仅模拟）-> live 由
    file_hash 重算（与 cache 文件名同域）-> 锚定 100% 保留；不在 manifest 的 orphan 被 sweep；
    semantic 同规则。"""
    root = tmp_path
    out = root / "graphify-out"
    ver = out / "cache" / "ast" / "vCUR"; ver.mkdir(parents=True)
    (out / "cache" / "semantic").mkdir(parents=True)
    (root / "a.py").write_text("print('a')", encoding="utf-8")
    (root / "b.py").write_text("print('b')", encoding="utf-8")
    (root / "orphan.py").write_text("print('orphan')", encoding="utf-8")   # 文件在但不在 manifest
    h_a, h_b, h_orphan = (file_hash(root / f, root=root) for f in ("a.py", "b.py", "orphan.py"))
    for h, mt in ((h_a, _RECENT), (h_b, _RECENT), (h_orphan, _OLD)):
        (ver / f"{h}.json").write_text("x" * 50)
        (out / "cache" / "semantic" / h).write_text("x" * 50)
        os.utime(ver / f"{h}.json", (mt, mt))
        os.utime(out / "cache" / "semantic" / h, (mt, mt))
    # manifest：值用 MD5 形态仅作模拟内容——live 由 file_hash 重算，不读 manifest 值
    (out / "manifest.json").write_text(json.dumps(
        {"a.py": {"mtime": 0, "ast_hash": f"{1:032x}", "semantic_hash": f"{2:032x}"},
         "b.py": {"mtime": 0, "ast_hash": f"{3:032x}", "semantic_hash": f"{4:032x}"}}),
        encoding="utf-8")
    live = cache_gc._recompute_live(root, out / "manifest.json")
    assert live == {h_a, h_b}                     # 重算锚定：manifest 文件 -> 同名域 hash
    r = _sweep(out, live, now_ts=_NOW)
    assert (ver / f"{h_a}.json").exists()         # 锚定命中：100% 保留
    assert (ver / f"{h_b}.json").exists()
    assert not (ver / f"{h_orphan}.json").exists()      # orphan 被 sweep
    assert (out / "cache" / "semantic" / h_a).exists()  # semantic 同规则
    assert not (out / "cache" / "semantic" / h_orphan).exists()
    assert r["live"] == 2

def test_gc_gate_real_amortization(tmp_path):
    """门控恢复真实摊销语义：live 非空后 n_entries > live*2+64 才触发（不再退化 64 floor）."""
    root = tmp_path
    out = root / "graphify-out"
    ver = out / "cache" / "ast" / "vCUR"; ver.mkdir(parents=True)
    (root / "a.py").write_text("print('a')", encoding="utf-8")
    h_a = file_hash(root / "a.py", root=root)
    (ver / f"{h_a}.json").write_text("x" * 10)
    for i in range(67):                           # 67 orphan + 1 live = 68 > 1*2+64=66 -> 触发
        f = ver / f"{'0' * 60}{i:04d}.json"
        f.write_text("x" * 10)
        os.utime(f, (_OLD, _OLD))
    (out / "manifest.json").write_text(json.dumps(
        {"a.py": {"mtime": 0, "ast_hash": "x", "semantic_hash": "y"}}), encoding="utf-8")
    r = gc_cache(out, out / "manifest.json", now_ts=_NOW, root=root)
    assert r["triggered"] is True                 # 68 > 1*2+64 -> 触发
    assert r["live"] == 1
    assert r["swept"] == 67                       # 67 orphan 全被 sweep，live 文件保留
    assert (ver / f"{h_a}.json").exists()

def test_manifest_deleted_file_skipped(tmp_path):
    """文件已删除的 manifest 条目被安全跳过（file_hash 抛错 -> 跳过，不 crash）."""
    root = tmp_path
    out = root / "graphify-out"; out.mkdir()
    (root / "a.py").write_text("print('a')", encoding="utf-8")
    h_a = file_hash(root / "a.py", root=root)
    (out / "manifest.json").write_text(json.dumps(
        {"a.py": {"mtime": 0, "ast_hash": "x", "semantic_hash": "y"},
         "gone.py": {"mtime": 0, "ast_hash": "z", "semantic_hash": "w"}}), encoding="utf-8")
    live = cache_gc._recompute_live(root, out / "manifest.json")
    assert live == {h_a}                          # gone.py 不存在 -> 跳过；a.py 正常重算
    r = gc_cache(out, out / "manifest.json", now_ts=_NOW, root=root)
    assert r["triggered"] is False                # n_entries=0 <= 1*2+64 -> 不触发，不 crash
