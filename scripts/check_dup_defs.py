"""merge 重复检查：模块级同名函数/类 + 与上游对比的重复复合语句块。

两类 merge 缺陷（0.9.92 merge 各踩中一次）：
  1. **同名函数/类被插入两次** —— Python 允许，后者静默覆盖前者
     （本次 7 处：paths/build/extract/llm/rust）
  2. **同一复合语句块被插入两次** —— 语法合法、不报错，但后者会重复执行
     （本次 1 处：build.py 的 post-build prune 块——第一处执行删除，
       第二处计数时节点已不在，`_pruned_node_count` 恒 0、消息退化为
       "no matching nodes or edges"）

判据：与 `upstream/v8` 对比——
  - 上游只有 1 份 → merge 造成，报错
  - 上游本身多份 → 原样保留（如 detect.py 的 `_nfc` 是两个同名不同用途的
                   上游函数；某些 if 块在上游也重复出现），不报
  - 无 upstream ref → 跳过判定，不误报（与 check-custom.sh 的输出面检查同款降级）

退出码：0 通过（或跳过）；1 发现问题。
"""
import ast
import hashlib
import pathlib
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8")

UPSTREAM_REF = "upstream/v8"
MIN_BLOCK_LINES = 5  # 更短的块容易巧合重复，不参与块指纹判定


def top_level_defs(src: str) -> dict[str, list[int]]:
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return {}
    names: dict[str, list[int]] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.setdefault(node.name, []).append(node.lineno)
    return names


def block_fingerprints(src: str) -> dict[str, list[int]]:
    """复合语句块（if/for/while/with/try）的源码指纹 → 出现行号。

    与 `top_level_defs` 互补：块没有名字，只能按内容哈希识别。
    0.9.92 merge 的 `build.py` post-build prune 块重复即此类。

    **已知盲区**（不检测，会误报）：
      - 嵌套函数/类重复（`ruby_resolution._promote_inferred` 即此类）——
        与"fork 新函数里的同名辅助函数"（如 serve 的 `_edge_at` 同时存在于
        fork 的 `_neighbors_lines` 和上游的 `_neighbor_lines`）无法靠内容哈希
        区分。这类需人工用 AST 函数级 diff 对比上游。
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return {}
    lines = src.splitlines()  # 预分割：避免 get_source_segment 逐节点重复切片
    fps: dict[str, list[int]] = {}
    for node in ast.walk(tree):
        if not isinstance(
            node,
            (ast.If, ast.For, ast.AsyncFor, ast.While,
             ast.With, ast.AsyncWith, ast.Try),
        ):
            continue
        # 行数预检：绝大多数节点在 5 行以下，先跳过再拼文本
        if node.end_lineno - node.lineno + 1 < MIN_BLOCK_LINES:
            continue
        seg = "\n".join(lines[node.lineno - 1:node.end_lineno]).rstrip()
        key = hashlib.sha256(seg.encode("utf-8")).hexdigest()
        fps.setdefault(key, []).append(node.lineno)
    return fps


def main() -> int:
    probe = subprocess.run(
        ["git", "rev-parse", "--verify", "-q", UPSTREAM_REF],
        capture_output=True,
    )
    if probe.returncode != 0:
        print(f"· 跳过（无 {UPSTREAM_REF} ref）")
        return 0

    cache: dict[str, str | None] = {}

    def upstream_src(path: str) -> str | None:
        if path not in cache:
            try:
                out = subprocess.run(
                    ["git", "show", f"{UPSTREAM_REF}:{path}"],
                    capture_output=True, text=True, encoding="utf-8", timeout=15,
                )
                cache[path] = out.stdout if out.returncode == 0 else None
            except (OSError, subprocess.SubprocessError):
                cache[path] = None
        return cache[path]

    issues = 0
    for path in sorted(pathlib.Path("graphify").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            src = path.read_text(encoding="utf-8")
        except OSError:
            continue
        up = upstream_src(path.as_posix())
        if up is None:
            continue  # 上游无此文件（fork 独有），无法判定
        up_defs = top_level_defs(up)
        up_blocks = block_fingerprints(up)

        for name, lines in top_level_defs(src).items():
            if len(lines) < 2:
                continue
            up_count = len(up_defs.get(name, []))
            if up_count == 1:
                print(f"  ✗ {path}:{lines} {name} —— 上游仅 1 处，疑似 merge 重复插入")
                issues += 1

        for key, lines in block_fingerprints(src).items():
            if len(lines) < 2:
                continue
            if len(up_blocks.get(key, [])) == 1:
                print(
                    f"  ✗ {path}:{lines} 复合语句块重复"
                    f"（{len(lines)} 份，上游仅 1 份）—— 疑似 merge 重复插入"
                )
                issues += 1

    return 1 if issues else 0


if __name__ == "__main__":
    sys.exit(main())
