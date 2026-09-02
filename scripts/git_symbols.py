"""C3：git 轴 changed_symbols（基线锚定 git_head + 孤儿 hash 回退 + 非 git no-op）。

git diff 文件集 -> codegraph DB 直查 nodes（file_path IN 文件集）-> 变更符号集。
from_head 由 serve 侧从状态文件 git_head 读出传入（G3 语义：字段缺失 -> from_head=None）；
非 git / git 不在 PATH -> git_available=False no-op；孤儿 hash（amend/rebase）-> 捕获报错
回退 graph_diff（basis='graph_diff'）——**本实现的 graph_diff = 无法锚定 git 基线的诚实
空标记，非"图内对比"**（无基线即无可靠变更信号；Task 11 C4 消费 basis 时以此为准）。
untracked 文件不计入变更集（git diff --name-only 语义——新文件未跟踪不属于 git diff）。

R5-1：git 命令一律 subprocess 直调（禁 shell 管道——Windows 无 grep/sort/uniq）。
"""
from __future__ import annotations
import sqlite3, subprocess, sys
from pathlib import Path

_QUERY_BATCH = 500   # IN 占位符分批上限（防 SQLite 变量上限 32766 临界时静默部分失败）


def _git(root: Path, *args) -> subprocess.CompletedProcess | None:
    """subprocess 直调 git（R5-1 纪律：无 shell 管道）。git 不在 PATH/启动失败 -> None."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    try:
        return subprocess.run(["git", *args], cwd=str(root), capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              creationflags=flags)
    except (OSError, subprocess.SubprocessError):
        return None


def git_available(root: Path) -> bool:
    """git 在 PATH 且 root 是 git 仓库（rev-parse --git-dir 成功即成立）."""
    r = _git(root, "rev-parse", "--git-dir")
    return r is not None and r.returncode == 0


def _changed_files_git(root: Path, from_head: str) -> list[str] | None:
    """git diff --name-only <from_head>..HEAD ∪ 工作区 diff（--name-only HEAD）-> 文件集。

    孤儿 hash（amend/rebase 后旧 commit 不可达）/git 报错 -> None（调用方回退 graph_diff）。
    去重保序（dict.fromkeys）：同一文件可能同时出现在两段 diff。git 输出是仓库相对路径
    （正斜杠），与 DB nodes.file_path 同口径（fork 实测 'graphify/__init__.py'）。"""
    r = _git(root, "diff", "--name-only", f"{from_head}..HEAD")
    if r is None or r.returncode != 0:
        return None
    files = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
    r2 = _git(root, "diff", "--name-only", "HEAD")
    if r2 is not None and r2.returncode == 0:
        files.extend(ln.strip() for ln in r2.stdout.splitlines() if ln.strip())
    # r2 失败静默——工作区 diff 是补充信息非基线语义（与孤儿 hash 的 stderr 告警路径
    # 不一致是有意的：基线失败要告警（f"{from_head}..HEAD" 返回 None），补充失败可静默）
    return list(dict.fromkeys(files))


def _query_symbols(root: Path, files: list[str]) -> list[dict]:
    """直查 DB nodes.file_path IN 文件集 -> [{id, label, file}]。

    label=COALESCE(qualified_name, name)（id 是 raw hash 无语义，显示用 qualified name）。
    返回 id 是 **DB raw id 坐标系**（codegraph nodes.id）——与 B 工具链消费的 merged 图
    节点 id 坐标系同源但非同构（__cg 消歧后缀/折叠差异）——不可直接用于 B 工具链，消费
    需经 label/坐标映射（Task 11 C4 亦然）。
    Minor-2：IN 占位符分批（_QUERY_BATCH=500/批）——现代 SQLite 变量上限 32766 不炸但
    超限会 OperationalError 被捕获 → 静默 0 符号 + basis 仍 git_head；分批防临界静默部分
    失败，超过一批时 stderr 一行告警（防静默）。DB 缺失/损坏/查询失败 -> 空（诚实降级，
    不崩出口——与 B3 M4 先例同向）。"""
    if not files:
        return []
    db = root / ".codegraph" / "codegraph.db"
    if not db.exists():
        return []
    out: list[dict] = []
    n_batches = 0
    try:
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        try:
            for i in range(0, len(files), _QUERY_BATCH):
                batch = files[i:i + _QUERY_BATCH]
                n_batches += 1
                ph = ",".join("?" * len(batch))
                rows = conn.execute(
                    "SELECT id, COALESCE(qualified_name, name), file_path "
                    f"FROM nodes WHERE file_path IN ({ph})", batch).fetchall()
                out.extend({"id": r[0], "label": r[1] or r[0], "file": r[2] or ""} for r in rows)
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        return []
    if n_batches > 1:
        print(f"[git_symbols] {len(files)} 个变更文件分 {n_batches} 批 IN 查询（防静默部分失败）",
              file=sys.stderr)
    return out


def changed_symbols(project_root, from_head=None) -> dict:
    """变更符号集。返回 {"files", "symbol_ids", "symbols", "basis", "git_available"}。

    basis="git_head"：锚定状态文件 git_head 基线（git diff 文件集 -> DB 符号集，精确）。
    basis="graph_diff"：无法锚定 git 基线（非 git / git_head 缺失 / 孤儿 hash）——无可靠
    变更信息，空结果（"没有变更信息"≠ok；serve 侧 N1 推导出 absent，C 信封纪律不走
    override）。symbols 为附加显示明细（id/label/file），不属文档契约最小键集。"""
    root = Path(project_root).resolve()
    if not git_available(root):
        print(f"[git_symbols] 非 git 仓库或 git 不可用（{root}）——no-op", file=sys.stderr)
        return {"files": [], "symbol_ids": [], "symbols": [], "basis": "graph_diff",
                "git_available": False}
    if from_head is None:
        # G3：状态文件无 git_head（首次运行/从未锚定）——无法锚定 git 基线，回退 graph_diff
        print("[git_symbols] git_head 缺失（基线未锚定）——回退 graph_diff", file=sys.stderr)
        return {"files": [], "symbol_ids": [], "symbols": [], "basis": "graph_diff",
                "git_available": True}
    files = _changed_files_git(root, from_head)
    if files is None:
        # 孤儿 hash（amend/rebase）——git 报错，回退 graph_diff + stderr 告警
        print(f"[git_symbols] git diff {from_head[:8]}..HEAD 失败（孤儿 hash？）"
              f"——回退 graph_diff", file=sys.stderr)
        return {"files": [], "symbol_ids": [], "symbols": [], "basis": "graph_diff",
                "git_available": True}
    symbols = _query_symbols(root, files)
    return {"files": files, "symbol_ids": [s["id"] for s in symbols],
            "symbols": symbols, "basis": "git_head", "git_available": True}
