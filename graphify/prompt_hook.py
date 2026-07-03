"""UserPromptSubmit hook 实现 - 在模型思考前注入图谱查询结果。

设计要点：
- 所有失败路径静默退出（exit 0，无 stdout 输出）
- 不引入 requests 等第三方依赖，使用标准库 urllib
- 捕获 _load_graph 的 SystemExit，绝不阻塞 Claude Code

v3 核查备注：
- _query_graph_text 第二个参数名是 question，v3 改用关键字调用 question=prompt
- _select_graph 签名 def _select_graph(project_path) -> None（副作用设置 active_graph_path）
- _load_graph 的 sys.exit(1) 在 serve.py:59,62
"""
import json
import os
import re
import sys
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError


# ── 结构性问题检测 ──────────────────────────────────────────────

STRUCTURAL_EN = re.compile(
    r"\b(how|where|trace|flow|path|reach(?:es|ed)?|"
    r"call(?:s|ed|er|ers|ee)?|depend|impact|affect|"
    r"wired?|connect|implement|architect|structure|"
    r"breaks?|what calls|why does|used by|uses)\b",
    re.IGNORECASE,
)

STRUCTURAL_CJK = re.compile(
    r"如何|怎么|在哪|哪里|追踪|跟踪|流程|流向|路径|"
    r"调用|依赖|影响|实现|架构|结构|介绍|解析|分析|"
    r"原理|机制|用到|使用|引用|关系|连接"
)

CODE_TOKEN_RE = re.compile(
    r"\b[a-zA-Z_][a-zA-Z0-9_]*(?:[.][a-zA-Z_][a-zA-Z0-9_]*)+\b|"
    r"\b[a-z]+[A-Z][a-zA-Z0-9]*\b|"
    r"\b[A-Z][a-z]+[A-Z][a-zA-Z0-9]*\b|"
    r"\b[a-z_][a-z0-9_]*_[a-z0-9_]+\b"
)


def _is_structural_question(prompt: str) -> bool:
    """检测是否为代码库结构性问题"""
    if STRUCTURAL_EN.search(prompt) or STRUCTURAL_CJK.search(prompt):
        return True
    tokens = CODE_TOKEN_RE.findall(prompt)
    return len(tokens) >= 1


# ── 项目查找 ────────────────────────────────────────────────────

def _is_workspace_root(path: Path) -> bool:
    markers = ["package.json", "pyproject.toml", "go.mod", "Cargo.toml", ".git"]
    return any((path / m).exists() for m in markers)


def _find_graphify_project(cwd: str) -> dict | None:
    """查找最近的 graph.json，支持 monorepo 子项目扫描。
    返回 {"graph_path": str, "project_root": str} 或 None。
    """
    cwd_path = Path(cwd).resolve()

    # 向上扫描：从 cwd 到根目录找 graphify-out/graph.json
    for parent in [cwd_path] + list(cwd_path.parents):
        gp = parent / "graphify-out" / "graph.json"
        if gp.is_file():
            return {"graph_path": str(gp), "project_root": str(parent)}

    # 向下 bounded BFS：如果 cwd 是 workspace root，扫描一层子目录
    if _is_workspace_root(cwd_path):
        found = []
        try:
            for p in cwd_path.rglob("graphify-out/graph.json"):
                if len(found) >= 5:
                    break
                found.append({
                    "graph_path": str(p),
                    "project_root": str(p.parent.parent),
                    "name": p.parent.parent.name,
                })
        except (PermissionError, OSError):
            pass
        if len(found) == 1:
            return found[0]
        if len(found) > 1:
            names = ", ".join(x["name"] for x in found)
            return {"multi_project": True, "names": names}

    return None


# ── 安全加载图（不 sys.exit）────────────────────────────────────

def _safe_load_graph(graph_path: str):
    """安全加载 graph.json，失败返回 None，绝不 sys.exit。

    _load_graph() 在文件不存在/JSON错误时会 sys.exit(1)（serve.py:59,62），
    在 hook 中这是致命的，所以需要捕获 SystemExit。
    """
    try:
        from graphify.serve import _load_graph, _get_trigram_index
        G = _load_graph(graph_path)
        _get_trigram_index(G)  # 预热索引
        return G
    except SystemExit:
        return None
    except Exception:
        return None


# ── 查询策略 ────────────────────────────────────────────────────

def _query_via_http(prompt: str, project_path: str) -> str | None:
    """通过 HTTP 调用常驻 MCP server 的 /query 端点（毫秒级）。
    使用标准库 urllib，不依赖 requests。
    v3 修订（审核 Bug 2）：补 project_path 参数，多项目场景避免查错图。
    v3 修订（审核优化 #4）：若配置了 GRAPHIFY_API_KEY，请求头带上 Authorization。
    v3 修订（实施修复）：传 project_path（项目根目录）而非 graph_path，
    因为 server 端 _select_graph 期望 project_path，内部用 _resolve_graph_path 解析。
    """
    port = os.environ.get("GRAPHIFY_MCP_PORT", "8765")
    url = f"http://127.0.0.1:{port}/query"
    timeout = float(os.environ.get("GRAPHIFY_HTTP_TIMEOUT", "5"))
    try:
        data = json.dumps({"prompt": prompt, "project_path": project_path}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        # v3 新增：API Key 校验客户端侧
        api_key = os.environ.get("GRAPHIFY_API_KEY", "").strip()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        req = Request(
            url, data=data,
            headers=headers,
            method="POST",
        )
        with urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                body = json.loads(resp.read().decode("utf-8"))
                return body.get("result")
    except (URLError, ConnectionError, OSError, json.JSONDecodeError, ValueError):
        pass
    return None


def _query_locally(prompt: str, graph_path: str) -> str | None:
    """本地直接加载 graph.json 查询（fallback，冷启动）。"""
    G = _safe_load_graph(graph_path)
    if G is None:
        return None
    try:
        from graphify.serve import _query_graph_text
        depth = int(os.environ.get("GRAPHIFY_PROMPT_HOOK_DEPTH", "2"))
        budget = int(os.environ.get("GRAPHIFY_PROMPT_HOOK_BUDGET", "3000"))
        # v3 修订（审核优化 #5）：改用关键字参数，更清晰
        return _query_graph_text(
            G, question=prompt, mode="bfs", depth=depth,
            token_budget=budget, context_filters=None,
        )
    except Exception:
        return None


def _query_graph(plan: dict, prompt: str) -> str | None:
    if plan.get("multi_project"):
        names = plan["names"]
        return (
            f"[graphify] 检测到多个 graphify 项目: {names}。"
            f"请指定具体项目或 cd 到该项目目录后再提问。"
        )

    graph_path = plan["graph_path"]
    project_root = plan.get("project_root", "")

    # 策略 1：HTTP MCP server（常驻，最快）
    if os.environ.get("GRAPHIFY_ALLOW_HTTP_MCP", "1") == "1":
        result = _query_via_http(prompt, project_root)  # v3 修订：传 project_root（server 端 _select_graph 期望项目根目录）
        if result:
            return result

    # 策略 2：本地加载（fallback）
    return _query_locally(prompt, graph_path)


# ── 入口 ────────────────────────────────────────────────────────

def prompt_hook_main() -> None:
    """UserPromptSubmit hook 入口。
    输入：stdin JSON（Claude Code 格式）
    输出：stdout 纯文本（自动追加到 Claude 上下文）；无内容则静默退出

    v3 修订（审核优化 #3）：支持 --test "<prompt>" [cwd] CLI 调试参数，
    绕过 isatty() 检查，便于手动测试。
    """
    # Kill switch
    if os.environ.get("GRAPHIFY_NO_PROMPT_HOOK") == "1":
        return

    # v3 新增：--test 参数用于 CLI 调试
    # 注意：graphify prompt-hook 调用时 sys.argv[1] 是 "prompt-hook"，--test 在后续位置
    if "--test" in sys.argv:
        test_idx = sys.argv.index("--test")
        prompt = sys.argv[test_idx + 1] if test_idx + 1 < len(sys.argv) else ""
        cwd = sys.argv[test_idx + 2] if test_idx + 2 < len(sys.argv) else os.getcwd()
    else:
        # TTY 环境不执行（手动调试时不会卡住）
        if sys.stdin.isatty():
            return

        # 解析输入
        try:
            raw = sys.stdin.read()
            input_data = json.loads(raw)
        except Exception:
            return

        prompt = str(input_data.get("prompt", ""))
        cwd = str(input_data.get("cwd", os.getcwd()))

    if not prompt:
        return

    # 结构性问题检测
    if not _is_structural_question(prompt):
        return

    # 查找 graphify 项目
    plan = _find_graphify_project(cwd)
    if not plan:
        return

    # 查询图谱
    result = _query_graph(plan, prompt)
    if not result:
        return

    # 截断输出
    max_bytes = int(os.environ.get("GRAPHIFY_PROMPT_HOOK_MAX_BYTES", "16000"))
    body = result[:max_bytes]
    if len(result) > max_bytes:
        body += "\n...(已截断，使用 `graphify query` 获取完整结果)"

    # 输出纯文本 → Claude Code 自动追加到上下文
    # v3 修订（审核优化 #6）：用 XML 标签让模型更易识别结构边界
    note = (
        "以下内容来自 graphify 知识图谱预查询结果。"
        "请直接基于此上下文回答，无需再 grep/read 源代码文件。"
    )
    sys.stdout.write(f"<graphify_context>\n{note}\n{body}\n</graphify_context>\n")


if __name__ == "__main__":
    prompt_hook_main()
