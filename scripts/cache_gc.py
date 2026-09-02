"""B4 cache GC：manifest 锚定 mark-and-sweep（方案 §5.5 七要点）.

安全性由构造保证：cache 按内容 hash 键，删任何条目最坏代价 = 下次 rebuild 重提取，
永不影响正确性。零参数面：宽限窗/阈值全部模块常量。

锚定方案（round 2 用户裁决）：live 重算——manifest 文件清单（相对项目根路径）逐条
file_hash 重算，产出与 cache 文件名同域（同函数同 path-salt），锚定天然成立；
manifest 值（MD5 形态）不参与锚定。sweep 范围 = cache/ast 当前版本目录
（非当前版本目录整删）+ cache/semantic（同规则）。"""
from __future__ import annotations
import json, sys, time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:            # 与 rebuild_entry.py 同先例：scripts 侧 import graphify 包
    sys.path.insert(0, str(_ROOT))

GRACE_WINDOW_S = 7 * 86400          # 3: 7 天宽限窗（git checkout/回退保护）
GATE_FACTOR, GATE_FLOOR = 2, 64     # 2: 频率门控 条目数 > live*2 + 64

# Step 3a: cache 版本目录名 = v{_EXTRACTOR_VERSION}-s{_AST_CACHE_SCHEMA}（graphify/cache.py:950，
# 实测目录名 v0.9.51+fork.1-s2 即此形态）。file_hash 是 cache.py 公开函数（:428），live 重算复用
# （零上游触碰）。import 失败则 _CACHE_VERSION=None 兜底（旧版本目录整删退化为仅文件级 sweep）
# + _file_hash=None（live 退化为空——异常环境降级，生产 scripts 上下文 graphify 恒可导入）。
try:
    from graphify.cache import _AST_CACHE_SCHEMA, _EXTRACTOR_VERSION, file_hash as _file_hash
    _CACHE_VERSION = f"v{_EXTRACTOR_VERSION}-s{_AST_CACHE_SCHEMA}"
except Exception:
    _CACHE_VERSION = None
    _file_hash = None


def _recompute_live(root: Path, manifest_path: Path) -> set[str]:
    """live 重算：manifest 文件清单（相对项目根路径）-> file_hash 重算 -> cache key 域锚定。

    manifest 值（MD5 形态）不参与——只取 keys（相对路径）；file_hash 产出与 cache 文件名
    同域（同函数同 path-salt），锚定天然成立。文件已删除/不可读的条目 file_hash 抛错，
    跳过该条目（文件都没了其 cache 该随宽限窗淘汰）。"""
    try:
        m = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    if _file_hash is None:
        return set()
    live: set[str] = set()
    for rel in m.keys():
        try:
            live.add(_file_hash(root / rel, root=root))
        except OSError:
            continue          # 文件已删除/不可读：跳过
    return live


def gc_cache(cache_root: Path, manifest_path: Path, now_ts: float | None = None,
             root: Path | None = None) -> dict:
    """门控判定 + 委托 _sweep（E3：生产签名无 force 后门，零参数面含函数参数面）.

    root = 项目根（manifest keys 相对此根，即 file_hash 的 path-salt 锚点）；默认取
    cache_root.parent（标准布局 root/graphify-out），rebuild_entry 挂载时显式传 root
    （--out-dir 自定义时 cache_root.parent 不是项目根）。"""
    now = now_ts if now_ts is not None else time.time()
    root = Path(root).resolve() if root is not None else cache_root.parent
    ast_root, sem_root = cache_root / "cache" / "ast", cache_root / "cache" / "semantic"
    live = _recompute_live(root, manifest_path)
    # 2: 频率门控——摊销扫描成本（live 非空后恢复真实摊销语义：条目数 > live*2+64 才触发）
    n_entries = sum(1 for d in (ast_root, sem_root) if d.is_dir()
                    for f in d.rglob("*") if f.is_file())
    if n_entries <= len(live) * GATE_FACTOR + GATE_FLOOR:
        return {"swept": 0, "live": len(live), "bytes_freed": 0, "triggered": False}
    r = _sweep(cache_root, live, now_ts=now)
    r["live"] = len(live); r["triggered"] = True
    return r


def _sweep(cache_root: Path, live: set[str], now_ts: float | None = None) -> dict:
    """mark-and-sweep 纯函数（E3：与门控解耦，测试直测；gc_cache 委托本函数）.

    sweep 范围 = cache/ast 当前版本目录（逐文件按 "文件名 ∉ live 且超宽限窗" 判删；
    非当前版本目录整删）+ cache/semantic（同规则）。live 由调用方重算（file_hash 同域
    锚定）：只有真 orphan（不在 manifest 文件清单、或文件内容已变）被删。"""
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
