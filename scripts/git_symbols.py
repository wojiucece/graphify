"""C3：git 轴 changed_symbols（基线锚定 git_head + 孤儿 hash 回退 + 非 git no-op）。

git diff 文件集 -> codegraph DB 直查 nodes（file_path IN 文件集）-> 变更符号集。
from_head 由 serve 侧从状态文件 git_head 读出传入（G3 语义：字段缺失 -> from_head=None）；
非 git / git 不在 PATH -> git_available=False no-op；孤儿 hash（amend/rebase）-> 捕获报错
回退 graph_diff（basis='graph_diff'）——无法锚定 git 基线即无可靠变更信息，最诚实形态是
空结果（serve 侧判 absent，不谎报 ok）。

R5-1：git 命令一律 subprocess 直调（禁 shell 管道——Windows 无 grep/sort/uniq）。
"""
from __future__ import annotations
import sqlite3, subprocess, sys
from pathlib import Path


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
    return list(dict.fromkeys(files))


def _query_symbols(root: Path, files: list[str]) -> list[dict]:
    """直查 DB nodes.file_path IN 文件集 -> [{id, label, file}]。

    label=COALESCE(qualified_name, name)（id 是 raw hash 无语义，显示用 qualified name）。
    DB 缺失/损坏/查询失败 -> 空（诚实降级，不崩出口——与 B3 M4 先例同向）。"""
    if not files:
        return []
    db = root / ".codegraph" / "codegraph.db"
    if not db.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        try:
            ph = ",".join("?" * len(files))
            rows = conn.execute(
                "SELECT id, COALESCE(qualified_name, name), file_path "
                f"FROM nodes WHERE file_path IN ({ph})", list(files)).fetchall()
            return [{"id": r[0], "label": r[1] or r[0], "file": r[2] or ""} for r in rows]
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        return []


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
