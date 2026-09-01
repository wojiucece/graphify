"""B4 cache GC：manifest 锚定 mark-and-sweep（方案 §5.5 七要点）.

安全性由构造保证：cache 按内容 hash 键，删任何条目最坏代价 = 下次 rebuild 重提取，
永不影响正确性。零参数面：宽限窗/阈值全部模块常量。"""
from __future__ import annotations
import json, sys, time
from pathlib import Path

GRACE_WINDOW_S = 7 * 86400          # 3: 7 天宽限窗（git checkout/回退保护）
GATE_FACTOR, GATE_FLOOR = 2, 64     # 2: 频率门控 条目数 > live*2 + 64

# Step 3a: cache 版本目录名 = v{_EXTRACTOR_VERSION}-s{_AST_CACHE_SCHEMA}（graphify/cache.py:950，
# 实测目录名 v0.9.51+fork.1-s2 即此形态）。定位到两个常量后 import 取用；import 失败
# （graphify 不可导入）则 _CACHE_VERSION=None 兜底——旧版本目录整删退化为仅 sweep 文件级。
try:
    from graphify.cache import _AST_CACHE_SCHEMA, _EXTRACTOR_VERSION
    _CACHE_VERSION = f"v{_EXTRACTOR_VERSION}-s{_AST_CACHE_SCHEMA}"
except Exception:
    _CACHE_VERSION = None


def _live_set(manifest_path: Path) -> set[str]:
    try:
        m = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    live = set()
    for v in m.values():
        for k in ("ast_hash", "semantic_hash"):
            if isinstance(v, dict) and v.get(k):
                live.add(str(v[k]))
    return live


def gc_cache(cache_root: Path, manifest_path: Path, now_ts: float | None = None) -> dict:
    """门控判定 + 委托 _sweep（E3：生产签名无 force 后门，零参数面含函数参数面）."""
    now = now_ts if now_ts is not None else time.time()
    ast_root, sem_root = cache_root / "cache" / "ast", cache_root / "cache" / "semantic"
    live = _live_set(manifest_path)
    # R5-2 等值假设探针（Step 3c）结论：fork 真实产物实测 manifest hash（MD5，32 hex）
    # 与 cache 文件名（SHA-256，64 hex）不等值（0.9.51+fork.1-s2 实测 517/517 失配）——
    # 等值假设不成立，按 brief 备选口径改用双向口径：live 限定为 cache 实际文件名 ∩
    # manifest 值。当前数据交集为空 -> live 空 -> 宽限窗后全量 sweep（纯性能回退，
    # 正确性无损：cache 按内容键，重删重提取；7 天宽限窗防 checkout 回退误删）。
    cache_stems = {f.stem for d in (ast_root, sem_root) if d.is_dir()
                   for f in d.rglob("*") if f.is_file()}
    live &= cache_stems
    # 2: 频率门控——摊销扫描成本，不是每次 rebuild 都扫
    n_entries = len(cache_stems)
    if n_entries <= len(live) * GATE_FACTOR + GATE_FLOOR:
        return {"swept": 0, "live": len(live), "bytes_freed": 0, "triggered": False}
    r = _sweep(cache_root, live, now_ts=now)
    r["live"] = len(live); r["triggered"] = True
    return r


def _sweep(cache_root: Path, live: set[str], now_ts: float | None = None) -> dict:
    """mark-and-sweep 纯函数（E3：与门控解耦，测试直测；gc_cache 委托本函数）.

    live 由调用方预解析（gc_cache 内已完成 cache 文件名 ∩ manifest 值的双向口径）；
    本函数只按 "文件名 ∉ live 且超宽限窗" 判删，保持纯函数面供测试直测。"""
    now = now_ts if now_ts is not None else time.time()
    ast_root, sem_root = cache_root / "cache" / "ast", cache_root / "cache" / "semantic"
    swept, freed = 0, 0

    def _sweep_file(p: Path):
        nonlocal swept, freed
        try:
            freed += p.stat().st_size; p.unlink(); swept += 1
        except OSError:
            pass

    if ast_root.is_dir():
        for ver_dir in ast_root.iterdir():
            if not ver_dir.is_dir():
                continue
            if _CACHE_VERSION and ver_dir.name != _CACHE_VERSION:
                for f in ver_dir.rglob("*"):     # 旧版本目录整删
                    if f.is_file(): _sweep_file(f)
                _rmdir_empty(ver_dir)
                continue
            for f in ver_dir.iterdir():
                if f.is_file() and f.stem not in live and now - f.stat().st_mtime > GRACE_WINDOW_S:
                    _sweep_file(f)
    if sem_root.is_dir():
        for f in sem_root.iterdir():
            if f.is_file() and f.stem not in live and now - f.stat().st_mtime > GRACE_WINDOW_S:
                _sweep_file(f)
    # 6: 遥测一行日志，不进状态文件
    print(f"[cache gc] swept {swept} orphans / {len(live)} live / {freed} bytes freed",
          file=sys.stderr)
    return {"swept": swept, "live": len(live), "bytes_freed": freed}


def _rmdir_empty(p: Path):
    try:
        next(p.iterdir())
    except StopIteration:
        p.rmdir()
    except OSError:
        pass
