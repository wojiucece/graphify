#!/usr/bin/env python3
"""§8 效率基准：合并栈（get_ranked_context + get_node）vs read/grep 脚本模拟.

对比口径（G4 非 agent 模拟——必须可复现；R4-3 执行环境 Windows 无 grep → 用 Python
等价实现；R5-1 禁 shell 管道 → 全程 subprocess/标准库，无 | 拼接）：

- (a) 合并栈：ranked_context(root, q) → top 结果；对 top-1 复刻 get_node
      include_source='body' 的输出（serve._format_node_card + _signature_line +
      _slice_source 同源装配）→ 累计 search 输出 + fetch 输出 token。
- (b) read/grep 模拟：Path(root).rglob("*.py") 收集文件 → 逐文件读内容做首 token
      子串扫描（`token in text`）得命中列表（≈ grep -rln）→ 按序模拟 agent 读取
      （每命中文件读头部 200 行）→ 命中期望符号所在文件则停 → tiktoken 累计
      grep 命中行 + 读取内容。

只量 token 与命中@5，不量耗时（耗时受本机 IO 影响不可复现；token 与检索排名是
纯函数口径）。基准前先 rebuild 保证同一起跑线（freshness=fresh，--skip-rebuild
可跳过，结果 JSON 会如实标注）。任务集 = 金标集（tests/fixtures/ranked_golden.json，
同一份人工标注服务两处：金标测试的期望符号集 + 本基准的检索任务）。

用法：
    python benchmarks/efficiency_benchmark.py [--root <path>]
        [--budget 2000] [--tasks 12] [--skip-rebuild] [--out benchmarks/results-<date>.json]
    root 解析优先级（L1）：--root 显式 > GRAPHIFY_GOLDEN_ROOT env > 本仓根（自指）。
    跑带 recall 的基准（金标 expect id 依赖 golden 根的 .fts-index.db）：GRAPHIFY_GOLDEN_ROOT=<新链路项目>

声明：默认 --rebuild 会对 --root 跑 scripts/rebuild_entry.py 全量重建（写 root 的
graphify-out/——graph.json 事实层 + .fts-index.db 派生缓存；Task 11 收尾后 codegraph
运行时/DB 已退役，fetch 与 recall 地面真值统一读 .fts-index.db nodes 元数据表，与
serve.get_node 同源。重建不改变未变代码的节点 id）。只读跑基准请 --skip-rebuild。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from datetime import date
from pathlib import Path

_BENCH_ROOT = Path(__file__).resolve().parent.parent          # 本 fork（worktree）
_GOLDEN_FILE = _BENCH_ROOT / "tests" / "fixtures" / "ranked_golden.json"
if str(_BENCH_ROOT) not in sys.path:
    sys.path.insert(0, str(_BENCH_ROOT))                      # graphify 包
if str(_BENCH_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(_BENCH_ROOT / "scripts"))          # scripts/ranked.py

from ranked import ranked_context, format_ranked, _count_tokens  # noqa: E402
from graphify import serve as _serve                            # noqa: E402

_READ_HEAD_LINES = 200      # read/grep 模拟：每命中文件读头部行数
_DEFAULT_TASKS = 12         # 固定任务数缺省（金标集前 N 条；--tasks 显式覆盖，不再封顶）


def _select_tasks(golden: list, n: int) -> list:
    """任务集切片：golden[:n]——--tasks 显式即生效（用户 M1 + SDD Minor-4：原写法在
    n >= 缺省 12 时把上限锁死为 12，--tasks 15 形同虚设）。"""
    return golden[:n]


def _positive_tasks(raw: str) -> int:
    """--tasks 校验（用户升格 1）：必须 >= 1——0/负会 golden[:0]=[] → n=0 → summary
    均值除零，且是跑完所有任务后才崩（浪费一轮基准）。argparse type 校验在参数解析期
    报错退出，绝不进入基准主体。"""
    try:
        n = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"--tasks 必须是正整数，got {raw!r}") from None
    if n < 1:
        raise argparse.ArgumentTypeError(f"--tasks 必须 >= 1，got {n}")
    return n


def _resolve_root(args_root: str | None) -> tuple[Path, str]:
    """root 解析（用户 L1 + SDD Imp-1）：--root 显式 > GRAPHIFY_GOLDEN_ROOT env >
    脚本所在仓根（benchmarks/ 的父目录——自指可复现，替代硬编码 D:/code/graphify_fork）。
    金标 expect id 依赖特定 DB（golden 根），跑带 recall 的基准请设 GRAPHIFY_GOLDEN_ROOT。"""
    if args_root:
        return Path(args_root).resolve(), "explicit --root"
    env = os.environ.get("GRAPHIFY_GOLDEN_ROOT", "").strip()
    if env:
        return Path(env).resolve(), "env GRAPHIFY_GOLDEN_ROOT"
    return _BENCH_ROOT, "benchmark repo root (self-referential)"


def _pinned_commit() -> str:
    """pinned commit：本 fork 当前 HEAD（单条命令，无管道）。"""
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=_BENCH_ROOT,
                             capture_output=True, text=True, timeout=30)
        return out.stdout.strip() if out.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def _run_rebuild(root: Path) -> dict:
    """基准前 rebuild（单一入口，子进程隔离——rebuild_entry 锁冲突/sync 失败会 sys.exit）。"""
    t0 = __import__("time").perf_counter()
    try:
        proc = subprocess.run(
            [sys.executable, str(_BENCH_ROOT / "scripts" / "rebuild_entry.py"),
             "--project", str(root)],
            capture_output=True, text=True, timeout=1800)
        elapsed = round(__import__("time").perf_counter() - t0, 1)
        return {"ran": True, "ok": proc.returncode == 0,
                "exit": proc.returncode, "elapsed_s": elapsed}
    except subprocess.TimeoutExpired:
        return {"ran": True, "ok": False, "exit": "timeout", "elapsed_s": 1800}
    except Exception as e:  # 重建失败绝不影响基准产出——标注后继续
        return {"ran": True, "ok": False, "exit": type(e).__name__, "elapsed_s": 0.0}


def _db_conn(root: Path) -> sqlite3.Connection | None:
    """新链路 fetch/recall 数据源：graphify-out/.fts-index.db nodes 元数据表
    （05 票三表之一，serve.get_node 同源点查）。codegraph DB 已退役——缺失返回 None
    （fetch/recall 降级为无 DB 分支，基准照跑）。"""
    db = root / "graphify-out" / ".fts-index.db"
    if not db.exists():
        return None
    return sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)


# ---------------------------------------------------------------------------
# (a) 合并栈：ranked_context + get_node(body) 复刻
# ---------------------------------------------------------------------------

def _merged_stack(root: Path, conn: sqlite3.Connection | None, G, query: str, budget: int) -> dict:
    """搜索（ranked_context 输出全文）+ 取 top-1 复刻 get_node body 输出，累计 token."""
    r = ranked_context(root, query, token_budget=budget)
    search_out = format_ranked(r)
    search_tokens, _ = _count_tokens(search_out)
    res = r.get("results", [])
    top5_ids = [x["id"] for x in res[:5]]
    fetch_tokens, fetch_label, fetch_ok = 0, None, False
    if res and conn is not None:
        nid = res[0]["id"]
        row = conn.execute(
            "SELECT source_file, source_location, end_line, end_byte, "
            "signature, docstring FROM nodes WHERE id = ?", (nid,)).fetchone()
        if row is not None and row[0]:
            d = G.nodes[nid] if (G is not None and nid in G) else {}
            # 用户 L3：合并图无 name 属性（R3-2 三次裁决事实），删死代码分支——
            # 只从 label 取末段（与 serve._symbol_short_name 同源口径）。
            short = str(d.get("label") or nid).rsplit(".", 1)[-1]
            card = [_serve._format_node_card(G, nid, d)] if (G is not None and nid in G) else [
                f"Node: {nid}"]
            sig = (row[4] or "").strip()
            doc = (row[5] or "").strip()
            sig_line = _serve._signature_line(sig, short) if sig else ""
            if sig_line:
                card.append(f"  Signature: {sig_line}")
            if doc:
                card.append(f"  Doc: {_serve.sanitize_label(doc.splitlines()[0])}")
            text, ok, _s, _e = _serve._slice_source(
                root, row[0], row[1], row[2], row[3], short,
                signature=sig_line or None, pad=0)
            if ok:
                card.append("Code:")
                card.extend(f"  {ln}" for ln in text.splitlines())
            else:
                card.append("Code: (slice unavailable — line drift detected; source body omitted)")
            fetch_out = "\n".join(card)
            fetch_tokens, _ = _count_tokens(fetch_out)
            fetch_label = str(d.get("label", nid)) if d else nid
            fetch_ok = ok
    return {
        "search_tokens": search_tokens,
        "fetch_tokens": fetch_tokens,
        "fetch_label": fetch_label,
        "fetch_ok": fetch_ok,
        "total_tokens": search_tokens + fetch_tokens,
        "top5_ids": top5_ids,
    }


def _recall(results_top5: list[str], expect: list[str], ids_only: bool = True) -> float:
    """命中@5：期望符号 id 出现在 top5 结果 id 中的比例（与金标测试 _recall 同口径）。"""
    hit = sum(1 for eid in expect if eid in results_top5)
    return hit / len(expect)


# ---------------------------------------------------------------------------
# (b) read/grep 脚本模拟（Python 等价，R4-3；零 shell 管道，R5-1）
# ---------------------------------------------------------------------------

def _grep_read_sim(root: Path, conn: sqlite3.Connection | None, expect: list[str],
                   query: str) -> dict:
    """首 token 子串扫描（≈grep -rln）+ 按序读命中文件头部 200 行，tiktoken 累计.

    命中检测 ground truth：expect 符号 id → .fts-index.db source_file（基准只读一次缓存做期望定位，
    模拟的搜索行为只用查询首 token，不泄漏图知识进搜索）。file: 前缀 id 直接取路径。
    """
    needle = query.split()[0] if query.split() else query
    # ground truth：期望符号所在文件
    gt_files: list[str] = []
    if conn is not None:
        for eid in expect:
            row = conn.execute("SELECT source_file FROM nodes WHERE id = ?", (eid,)).fetchone()
            if row and row[0]:
                gt_files.append(row[0].replace("\\", "/"))
            elif eid.startswith("file:"):
                gt_files.append(eid[len("file:"):].replace("\\", "/"))
    gt_set = set(gt_files)

    # 1) rglob 收集 *.py → 逐文件首 token 子串扫描得命中列表（≈ grep -rln）
    hit_files: list[str] = []
    matched_lines: list[str] = []
    try:
        for p in root.rglob("*.py"):
            rel = p.relative_to(root).as_posix()
            if ".venv" in rel or "/build/" in rel or rel.startswith("build/"):
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if needle in text:
                hit_files.append(rel)
                matched_lines.extend(
                    ln for ln in text.splitlines() if needle in ln)
    except OSError:
        pass

    # 2) 按序模拟 agent 读取（每命中文件读头部 200 行），命中期望符号则停
    read_tokens = 0
    read_files: list[str] = []
    rank = None
    for rel in hit_files:
        read_files.append(rel)
        try:
            lines = (root / rel).read_text(encoding="utf-8", errors="ignore").splitlines()[:_READ_HEAD_LINES]
        except OSError:
            lines = []
        read_tokens += _count_tokens("\n".join(lines))[0]
        if rel in gt_set:
            rank = len(read_files)
            break
    grep_tokens = _count_tokens("\n".join(matched_lines))[0]
    return {
        "needle": needle,
        "grep_hit_files": len(hit_files),
        "grep_tokens": grep_tokens,
        "read_files": len(read_files),
        "read_tokens": read_tokens,
        "total_tokens": grep_tokens + read_tokens,
        "hit5": 1.0 if rank is not None and rank <= 5 else 0.0,
        "rank": rank,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="§8 效率基准：合并栈 vs read/grep（token/命中@5）")
    ap.add_argument("--root", default=None,
                    help="代码根（显式最高优先；缺省 GRAPHIFY_GOLDEN_ROOT env，再缺省本仓根）")
    ap.add_argument("--budget", type=int, default=2000, help="ranked_context token_budget")
    ap.add_argument("--tasks", type=_positive_tasks, default=_DEFAULT_TASKS,
                    help="金标集任务数（默认前 12，显式全量生效；>=1，0/负报参数错误）")
    ap.add_argument("--rebuild", dest="rebuild", action="store_true", default=True,
                    help="基准前对 --root 跑 rebuild_entry 全量重建（默认开，保证同一起跑线）")
    ap.add_argument("--skip-rebuild", dest="rebuild", action="store_false",
                    help="跳过 rebuild（.fts-index.db 已 fresh 的只读跑用；结果 JSON 如实标注）")
    ap.add_argument("--out", default=None, help="结果 JSON 路径（默认 benchmarks/results-<date>.json）")
    args = ap.parse_args()

    root, root_source = _resolve_root(args.root)
    golden = json.loads(_GOLDEN_FILE.read_text(encoding="utf-8"))
    tasks = _select_tasks(golden, args.tasks)

    rebuild_info = _run_rebuild(root) if args.rebuild else {
        "ran": False, "ok": None, "exit": "skipped",
        "elapsed_s": 0.0, "note": ".fts-index.db 与任务集已核对 fresh（golden 门过）",
    }

    conn = _db_conn(root)
    # 合并栈 fetch 需要图（_format_node_card/_slice_source）——用 serve._load_graph
    # （nx.Graph，与 get_node handler 的 G 同源），不是 ranked._load_graph（B1 视图返回
    # (degree, collision_bases, nodes) 元组，无 .nodes/.degree）。
    G = None
    if conn is not None:
        try:
            G = _serve._load_graph(str(root / "graphify-out" / "graph.json"))
        except Exception:
            G = None

    rows = []
    for item in tasks:
        q = item["q"]
        expect = item["expect"]
        merged = _merged_stack(root, conn, G, q, args.budget)
        merged["hit5"] = _recall(merged.pop("top5_ids"), expect)
        gr = _grep_read_sim(root, conn, expect, q)
        rows.append({"q": q, "type": item.get("type"), "expect": expect, "merged": merged, "grep_read": gr})

    n = len(rows)
    m_total = sum(r["merged"]["total_tokens"] for r in rows)
    g_total = sum(r["grep_read"]["total_tokens"] for r in rows)
    m_hit5 = sum(r["merged"]["hit5"] for r in rows) / n
    g_hit5 = sum(r["grep_read"]["hit5"] for r in rows) / n
    summary = {
        "tasks": n,
        "merged_total_tokens": m_total,
        "grep_read_total_tokens": g_total,
        "merged_mean_hit5": round(m_hit5, 3),
        "grep_read_mean_hit5": round(g_hit5, 3),
        "merged_pct_of_grep_read": round(100.0 * m_total / g_total, 1) if g_total else None,
        # SDD Minor-2：两臂命中@5 口径不同，不可直接比——merged=expect 集上 recall
        # 分数均值（0..1 连续值）；grep_read=前 5 次读取内命中首期望文件的二值均值。
        "hit5_note": ("merged=expect 集上 recall 分数均值（连续）；grep_read=前 5 次读取内"
                      "命中首期望文件的二值均值——两臂口径不同，不可直接比较"),
    }

    _date = date.today().isoformat()
    out_path = Path(args.out) if args.out else _BENCH_ROOT / "benchmarks" / f"results-{_date}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    result = {
        "benchmark": "efficiency_benchmark",
        "date": _date,
        "pinned_commit": _pinned_commit(),
        "root": str(root),
        "root_source": root_source,
        "token_count": _count_tokens("")[1],   # tiktoken | estimate（declared）
        "rebuild": rebuild_info,
        "task_set": {"source": "tests/fixtures/ranked_golden.json",
                     "slice": f"first {n} entries", "n": n},
        "tasks": rows,
        "summary": summary,
    }
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "pinned_commit": result["pinned_commit"],
        "rebuild": rebuild_info,
        "summary": summary,
    }, ensure_ascii=False, indent=2))
    print(f"results -> {out_path}")


if __name__ == "__main__":
    main()
