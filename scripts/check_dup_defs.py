"""merge 重复定义检查：模块级同名函数/类出现多次，且上游只有一处。

背景：0.9.92 merge 时同一段代码被插入两次（上游定义数均为 1）。Python 允许
同模块重复定义、后者**静默覆盖**前者——不报错、不告警。本次 6 对内容相同的
无行为影响，但 `_resolve_kotlin_member_calls` 那对不同（84 行旧版 vs 92 行
上游新版），旧版若排在后面就会静默吃掉上游的新功能（跨仓 park）。

判据：与 `upstream/v8` 对比——
  - 上游只有 1 处  → merge 造成，报错
  - 上游本身多处  → 原样保留（如 detect.py 的 `_nfc` 是两个同名不同用途的
                     上游函数），不报
  - 无 upstream ref → 跳过判定，不误报（与 check-custom.sh 的输出面检查同款降级）

退出码：0 通过（或跳过）；1 发现 merge 遗留重复。
"""
import ast
import pathlib
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8")

UPSTREAM_REF = "upstream/v8"


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


def upstream_def_count(path: str, name: str) -> int | None:
    try:
        out = subprocess.run(
            ["git", "show", f"{UPSTREAM_REF}:{path}"],
            capture_output=True, text=True, encoding="utf-8", timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0 or not out.stdout:
        return None
    return len(top_level_defs(out.stdout).get(name, []))


def main() -> int:
    probe = subprocess.run(
        ["git", "rev-parse", "--verify", "-q", UPSTREAM_REF],
        capture_output=True,
    )
    if probe.returncode != 0:
        print(f"· 跳过（无 {UPSTREAM_REF} ref）")
        return 0

    issues = 0
    for path in sorted(pathlib.Path("graphify").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            src = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for name, lines in top_level_defs(src).items():
            if len(lines) < 2:
                continue
            up = upstream_def_count(path.as_posix(), name)
            if up == 1:
                print(f"  ✗ {path}:{lines} {name} —— 上游仅 1 处，疑似 merge 重复插入")
                issues += 1
            elif up is None:
                print(f"  · {path}:{lines} {name} —— 上游无该文件，无法判定（跳过）")
            # up >= 2：上游本身多处同名，原样保留，不报
    return 1 if issues else 0


if __name__ == "__main__":
    sys.exit(main())
