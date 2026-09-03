"""C3：git 轴 changed_symbols（基线锚定 git_head + 孤儿 hash 回退 + 非 git no-op）。

git diff 文件集 -> graph.json 内存索引（source_file 建索引 O(n)）-> 变更符号集。
from_head 由 serve 侧从状态文件 git_head 读出传入（G3 语义：字段缺失 -> from_head=None）；
非 git / git 不在 PATH -> git_available=False no-op；孤儿 hash（amend/rebase）-> 捕获报错
回退 graph_diff（basis='graph_diff'）——**本实现的 graph_diff = 无法锚定 git 基线的诚实
空标记，非"图内对比"**（无基线即无可靠变更信号；Task 11 C4 消费 basis 时以此为准）。
untracked 文件不计入变更集（git diff --name-only 语义——新文件未跟踪不属于 git diff）。

C4：hotspots（Task 11）——churn（git log --name-only 文件 commit 频次）× 度数代理
（graph.json 合并图边按双端点 source_file 计数，fan-in 纳入）交叉积排序 top-N。
06 票换源：codegraph DB 退役，符号/度数均取 graph.json 唯一事实层；度数单位 = 合并图
边计数（与 god_nodes/B1 同单位，旧链路 raw 边计数未折叠的单位差异按决议诚实标注——
见 serve._format_hotspots 头行）。两个轴都是 declared 代理值（无文件行数/圈复杂度
属性，不假装有复杂度信号）。

R5-1：git 命令一律 subprocess 直调（禁 shell 管道——Windows 无 grep/sort/uniq）。
"""
from __future__ import annotations
import json, os, subprocess, sys
from collections import Counter
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
    # r2 失败静默——工作区 diff 是补充信息非基线语义（与孤儿 hash 的 stderr 告警路径
    # 不一致是有意的：基线失败要告警（f"{from_head}..HEAD" 返回 None），补充失败可静默）
    return list(dict.fromkeys(files))


def _load_graph_json(root: Path) -> dict | None:
    """graph.json 内存加载（唯一事实层）。GRAPHIFY_OUT 重定向跟随（环境变量默认
    graphify-out，与 serve 路径联动总条款同源）。缺失/损坏 -> None（诚实降级，不崩出口）。"""
    out = os.environ.get("GRAPHIFY_OUT", "graphify-out")
    p = Path(root) / out / "graph.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _is_file_node(node: dict) -> bool:
    """file 节点判定：显式 kind='file' 或 label == source_file 的 basename /
    directory-qualified 后缀（graphify 原生 AST 路径——文件节点 label == basename）。
    与 graphify.build._is_file_node_label 同源（scripts 无包结构自包含副本，防漂移靠
    fixture 派生测试）。"""
    if node.get("kind") == "file":
        return True
    label = node.get("label") or ""
    source_file = node.get("source_file") or ""
    if not label or not source_file:
        return False
    sf = str(source_file).replace("\\", "/")
    lbl = str(label)
    if lbl == sf.rsplit("/", 1)[-1]:
        return True
    return "/" in lbl and (sf == lbl or sf.endswith("/" + lbl))


def _query_symbols(root: Path, files: list[str]) -> list[dict]:
    """graph.json 内存索引（source_file 建索引 O(n)）——文件集内符号节点（file 节点剔除：
    变更符号 = 文件内代码符号，文件节点自身不是符号）。

    label = COALESCE(qualified_name, label)（id 是 hash 形态无语义，显示用 qualified
    name——与旧链路 DB COALESCE(qualified_name, name) 同构）。返回 id 是图节点原生
    kind+hash 形态——与 B 工具链消费的 merged 图节点 id 坐标系同源可直接用（旧链路
    DB raw id 的 __cg/折叠差异随 codegraph 退役消失）。graph.json 缺失/损坏 -> 空
    （诚实降级，不崩出口——与 B3 M4 先例同向）。"""
    g = _load_graph_json(root)
    if not files or not g:
        return []
    by_file: dict[str, list[dict]] = {}
    for n in g.get("nodes", []):
        if not isinstance(n, dict):
            continue
        sf = n.get("source_file")
        if sf:
            by_file.setdefault(sf, []).append(n)
    out: list[dict] = []
    for f in files:
        for n in by_file.get(f, []):
            if _is_file_node(n):
                continue
            out.append({"id": n.get("id", ""),
                        "label": n.get("qualified_name") or n.get("label") or n.get("id", ""),
                        "file": f})
    return out


def changed_symbols(project_root, from_head=None) -> dict:
    """变更符号集。返回 {"files", "symbol_ids", "symbols", "basis", "git_available"}。

    basis="git_head"：锚定状态文件 git_head 基线（git diff 文件集 -> graph.json 符号集，精确）。
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


# === CUSTOM: C4 hotspots（Task 11）============================================


def _churn(root: Path) -> dict[str, int]:
    """文件 commit 频次：git log --name-only 全历史，subprocess 取 stdout 后纯 Python
    计数（R5-1：无 shell 管道依赖——Windows 无 grep/sort/uniq，R4-3 同族问题）。

    core.quotepath=off：git 默认对非 ASCII 路径八进制转义+引号包裹，会与 DB
    nodes.file_path 坐标 join 断裂（用户仓实际含中文路径文件）。git log 失败（空仓库
    无提交/git 不可用）-> 空 Counter。文件按提交频次计（重命名路径不合并——churn 语义
    即"该路径被提交的次数"）。"""
    r = _git(root, "-c", "core.quotepath=off", "log", "--name-only", "--pretty=format:")
    if r is None or r.returncode != 0:
        return Counter()
    return Counter(ln.strip() for ln in r.stdout.splitlines() if ln.strip())


def _degree(root: Path) -> dict[str, int] | None:
    """合并图度数代理：graph.json links 按双端点 source_file（source ∪ target）计数。

    06 票换源（codegraph DB 退役）：source 端与 target 端各按端点 source_file 计数后
    相加（**fan-in 纳入**——"常改 × 波及大"的波及 = 别人依赖你，interfaces/models 类
    高 fan-in 低 fan-out 文件 source-only 下永远成不了热区，方向反了）；方向语义与
    god_nodes/B1 合并图度数（in+out）对齐（跨工具数字一致性）。**单位注释（06 决议）**：
    此处是 graph.json 合并图边计数——已折叠（同 (source,target) pair 的多条 raw 边在
    合并图只计一次），与 god_nodes/B1 同单位（旧链路是 codegraph DB raw 边计数未折叠，
    存在单位差异——诚实标注见 serve._format_hotspots 头行）。自环边两端同文件计 2
    （与合并图 nx 自环度数惯例一致）。端点无 source_file 按可归属端归组（两端都无则
    不计，宁少标）。**declared 代理**：这是连接度，不是圈复杂度——graph.json 无文件
    行数属性（方案 §5-C4 实测条款）。graph.json 缺失/损坏 -> None（调用方标注
    degree_available=False）。一次加载全量取回，无 N+1。"""
    g = _load_graph_json(root)
    if not g:
        return None
    file_of = {n.get("id"): n.get("source_file") for n in g.get("nodes", [])
               if isinstance(n, dict) and n.get("source_file")}
    deg: Counter = Counter()
    for e in g.get("links", []):
        if not isinstance(e, dict):
            continue
        sf = file_of.get(e.get("source"))
        tf = file_of.get(e.get("target"))
        if sf:
            deg[sf] += 1
        if tf:
            deg[tf] += 1
    return {fp: int(c) for fp, c in deg.items() if fp}


def hotspots(project_root, top_n=10) -> dict:
    """热区 top-N：score = churn（git log 文件 commit 频次）× 度数（graph.json 合并图
    边按双端点 source_file 计数——I1：fan-in 纳入，与 god_nodes/B1 合并图度数 in+out
    同方向同单位，06 票换源后已对齐）。返回 {"hotspots": [...], "git_available",
    "scanned", "degree_available"}。

    每个条目 {"file", "churn", "degree", "score"}。**declared 代理纪律**：churn 与度数
    都是代理值（graph.json 无文件行数/圈复杂度属性，不假装有复杂度信号——方案 §5-C4
    实测条款）；git log 与 graph.json source_file 同坐标（仓库相对正斜杠路径）。

    scanned = 参与排序的文件数 = churn>0 的文件数（度数缺位的 churn 文件以 degree=0
    参与排序）；hotspots 仅含 score>0 条目——热区需要两个轴都有信号，单轴无法定位
    热区。排序 (score, churn) 降序 + file 升序平局裁决（确定性）。非 git / git 不可用
    -> git_available=False no-op（无 churn 轴 -> 无热区信息）。"""
    root = Path(project_root).resolve()
    top_n = max(0, int(top_n))   # 负值钳制（防 [:负数] 切片反转语义截错尾）
    if not git_available(root):
        print(f"[git_symbols] 非 git 仓库或 git 不可用（{root}）——hotspots no-op",
              file=sys.stderr)
        return {"hotspots": [], "git_available": False, "scanned": 0,
                "degree_available": False}
    churn = _churn(root)
    degree = _degree(root)
    if churn and degree is None:
        print("[git_symbols] graph.json 缺失/不可读——度数轴缺失，无法交叉积排序",
              file=sys.stderr)
    deg = degree or {}   # graph.json 缺失 -> 空度数表（churn 文件全以 degree=0 参与排序）
    pool = [(f, c, deg.get(f, 0), c * deg.get(f, 0)) for f, c in churn.items()]
    pool.sort(key=lambda t: (-t[3], -t[1], t[0]))
    results = [{"file": f, "churn": c, "degree": d, "score": s}
               for f, c, d, s in pool if s > 0][:top_n]
    return {"hotspots": results, "git_available": True, "scanned": len(churn),
            "degree_available": degree is not None}
