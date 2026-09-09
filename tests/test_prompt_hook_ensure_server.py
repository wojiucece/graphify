"""serve-memory 票 02：R3 prompt-hook 自愈闭环——server 死后非阻塞拉起 + 本地回退。

spec: docs/specs/serve-memory-spec.md §R3（:61-68）与 §Testing 验收 6（:103）与
用户发现 3（prompt_hook 失败分支测试——该文件现零测试保护，本票起建立回归网）。

覆盖：
- 验收 6（单元/回归网）：mock urlopen 抛连接异常 → ensure-server 非阻塞调用恰一次 +
  该条返回本地回退结果 + 下条恢复 HTTP 状态语义
- 成功 HTTP 不触发 ensure-server（server 活着不误拉起）
- _ensure_server 非阻塞（Popen 不 wait）+ 脚本缺失/异常静默（hook 绝不因自愈卡 prompt）
- 脚本单一事实源：ensure-graphify-server.sh 含 health-probe（--max-time）+ launch-marker
  防抖 + nohup --watch 启动 + 复活 default 漂移注记；sessionstart 委托共享脚本
- 验收 6（E2E 实测）：真实 server 死后首条 prompt 本地回退 + ensure-server 拉起一次；
  次条恢复 HTTP（真实结果非本地哨兵）
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


# === 测试辅助 ===

def _plan(project_root: str = "P", graph_path: str = "g.json") -> dict:
    return {"graph_path": graph_path, "project_root": project_root}


class _Resp:
    """urlopen 成功响应替身（status=200 + JSON body）。"""

    def __init__(self, result: str):
        self.status = 200
        self._body = json.dumps({"result": result}).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


def _install_fake_urlopen(monkeypatch, sequence):
    """把 prompt_hook.urlopen 换成脚本化序列（依次返回 _Resp 或抛 URLError）。"""
    import graphify.prompt_hook as ph
    from urllib.error import URLError
    state = {"n": 0}

    def fake_urlopen(req, timeout=None):
        i = state["n"]
        state["n"] += 1
        item = sequence[min(i, len(sequence) - 1)]
        if isinstance(item, type) and issubclass(item, Exception):
            raise item("boom")
        return item

    monkeypatch.setattr(ph, "urlopen", fake_urlopen)
    return state


# === 验收 6（单元/回归网）：失败分支 → ensure 恰一次 + 本地回退 + 下条恢复 HTTP ===

def test_http_failure_triggers_ensure_once_then_local_fallback(monkeypatch):
    """mock urlopen 抛连接异常（首条）→ ensure-server 非阻塞恰一次 + 该条返回本地回退；
    次条 urlopen 成功 → 恢复 HTTP 结果（状态语义：server 拉起后 HTTP 优先）。"""
    import graphify.prompt_hook as ph
    from urllib.error import URLError
    ensure_calls = []
    monkeypatch.setattr(ph, "_ensure_server", lambda cwd: ensure_calls.append(cwd))
    monkeypatch.setattr(ph, "_query_locally", lambda prompt, graph_path: "LOCAL-RESULT")
    _install_fake_urlopen(monkeypatch, [URLError, _Resp("HTTP-RESULT")])
    plan = _plan()
    monkeypatch.setenv("GRAPHIFY_ALLOW_HTTP_MCP", "1")

    r1 = ph._query_graph(plan, "q?")
    assert r1 == "LOCAL-RESULT", "首条（HTTP 失败）应返回本地回退结果"
    assert ensure_calls == [plan["project_root"]], "失败分支应恰好调用一次 ensure-server"

    r2 = ph._query_graph(plan, "q?")
    assert r2 == "HTTP-RESULT", "次条应恢复 HTTP（server 已拉起）"
    assert ensure_calls == [plan["project_root"]], "HTTP 恢复后不应再调 ensure-server"


def test_http_success_does_not_trigger_ensure(monkeypatch):
    """HTTP 成功不触发 ensure-server（server 活着不误拉起——自愈只响应失败）。"""
    import graphify.prompt_hook as ph
    ensure_calls = []
    monkeypatch.setattr(ph, "_ensure_server", lambda cwd: ensure_calls.append(cwd))
    _install_fake_urlopen(monkeypatch, [_Resp("HTTP-RESULT")])
    plan = _plan()
    monkeypatch.setenv("GRAPHIFY_ALLOW_HTTP_MCP", "1")
    assert ph._query_graph(plan, "q?") == "HTTP-RESULT"
    assert ensure_calls == [], "HTTP 成功不应触发 ensure-server"


def test_http_non200_no_ensure_but_local_fallback(monkeypatch):
    """HTTP 200 之外（如 500/404，server 活着）不触发 ensure——拉起只响应连接级失败。"""
    import graphify.prompt_hook as ph

    class _Resp500(_Resp):
        def __init__(self):
            self.status = 500
            self._body = b'{"error": "boom"}'

    ensure_calls = []
    monkeypatch.setattr(ph, "_ensure_server", lambda cwd: ensure_calls.append(cwd))
    monkeypatch.setattr(ph, "_query_locally", lambda prompt, graph_path: "LOCAL-RESULT")
    _install_fake_urlopen(monkeypatch, [_Resp500()])
    plan = _plan()
    monkeypatch.setenv("GRAPHIFY_ALLOW_HTTP_MCP", "1")
    assert ph._query_graph(plan, "q?") == "LOCAL-RESULT"
    assert ensure_calls == [], "HTTP 非连接失败（server 活着）不应触发 ensure-server"


# === _ensure_server 非阻塞 + 静默 ===

def test_ensure_server_nonblocking_and_silent(tmp_path, monkeypatch):
    """_ensure_server 非阻塞（Popen 不 wait，nohup 后台）+ 脚本缺失/异常静默不抛。"""
    import graphify.prompt_hook as ph
    pops = []

    class _Popen:
        def __init__(self, *a, **k):
            pops.append((a, k))

    monkeypatch.setattr(ph.subprocess, "Popen", _Popen)
    monkeypatch.setattr(ph, "_default_ensure_script", lambda: "/nonexistent/ensure.sh")
    ph._ensure_server("/proj")                       # 脚本不存在 → 静默返回
    assert pops == [], "脚本缺失时 _ensure_server 应静默跳过"

    script = tmp_path / "ensure-graphify-server.sh"
    script.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    monkeypatch.setattr(ph, "_default_ensure_script", lambda: str(script))
    ph._ensure_server("/proj")
    assert len(pops) == 1, "脚本存在时应发起一次拉起（Popen）"
    args, kwargs = pops[0]
    assert args[0][0] == "bash" and args[0][1] == str(script) and args[0][2] == "/proj", \
        f"应 bash <script> <cwd>，got {args[0]}"
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["stdout"] == subprocess.DEVNULL
    assert kwargs["stderr"] == subprocess.DEVNULL, "三流全 DEVNULL（非阻塞静默）"


def test_default_ensure_script_points_to_repo_scripts():
    """_default_ensure_script 指向 fork 仓库 scripts/ensure-graphify-server.sh（存在）。"""
    from graphify import prompt_hook as ph
    script = ph._default_ensure_script()
    assert script and script.endswith(("scripts", "ensure-graphify-server.sh")), \
        f"应指向 scripts/ensure-graphify-server.sh，got {script!r}"
    assert Path(script).exists()


def test_ensure_server_script_has_probe_debounce_and_launch():
    """ensure-graphify-server.sh 单一事实源：health-probe（--max-time）+ launch-marker 防抖
    + nohup --watch 启动 + 复活 default 漂移注记。"""
    repo = Path(__file__).resolve().parent.parent
    script = repo / "scripts" / "ensure-graphify-server.sh"
    assert script.exists(), "ensure-graphify-server.sh 应存在（单一事实源）"
    text = script.read_text(encoding="utf-8")
    assert "curl" in text and "--max-time" in text, "探活 curl 需 --max-time 上限（防挂死）"
    assert "/health" in text
    assert "graphify-serve.launch" in text, "launch-marker 防抖应存在"
    assert "nohup" in text and "--watch" in text and "graphify-mcp" in text, \
        "nohup + graphify-mcp --watch 启动应存在（启动行为不变）"
    assert "default" in text.lower(), "复活 default 漂移裁决应有注记"


def test_sessionstart_delegates_to_ensure_script():
    """sessionstart-graphify-server.sh 改调共享脚本 ensure-graphify-server.sh（启动行为不变）。"""
    repo = Path(__file__).resolve().parent.parent
    script = repo / "scripts" / "sessionstart-graphify-server.sh"
    assert script.exists()
    text = script.read_text(encoding="utf-8")
    assert "ensure-graphify-server.sh" in text, "sessionstart 应委托共享脚本（单一事实源）"


# === 验收 6（E2E 实测）：server 死后首条本地回退 + ensure 恰一次；次条恢复 HTTP ===

def _mini_proj(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "a.py").write_text(
        "import b\n\ndef foo():\n    return b.bar()\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("def bar():\n    return 1\n", encoding="utf-8")
    return tmp_path


def _free_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_http(url: str, timeout: float = 25.0, interval: float = 0.2) -> bool:
    import urllib.request
    deadline = __import__("time").time() + timeout
    while __import__("time").time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        __import__("time").sleep(interval)
    return False


def _kill_port(port: int) -> None:
    """kill 监听指定端口的进程（Windows: netstat 找 PID + taskkill /T /F）。"""
    import subprocess as sp
    try:
        out = sp.check_output(["netstat", "-ano"], text=True, errors="replace")
    except Exception:
        return
    pids = set()
    for line in out.splitlines():
        parts = line.split()
        if (len(parts) >= 5 and parts[0].endswith(f":{port}")
                and parts[3] == "LISTENING"):
            pids.add(parts[4])
    for pid in pids:
        try:
            sp.run(["taskkill", "/PID", pid, "/T", "/F"],
                   stdout=sp.DEVNULL, stderr=sp.DEVNULL)
        except Exception:
            pass


def test_self_heal_prompt_after_server_death(tmp_path, monkeypatch):
    """验收 6（E2E 实测）：真实 server 启动 → 首次 HTTP 查询成功 → kill server → 首条
    prompt 本地回退 + ensure-server 拉起一次（真实脚本，防抖背书）→ 等新 server 就绪 →
    次条 prompt 恢复 HTTP（真实结果，非本地哨兵）。

    环境守卫：git-bash / curl / graphify-mcp 任一不可用则 skip（脚本自愈是 POSIX 语义，
    无 git-bash 的 Windows 环境无法实证）。venv Scripts 前置 PATH，保证拉起的 server 是
    worktree 源码（editable）。
    """
    pytest.importorskip("mcp")
    import time
    import urllib.request
    import graphify.prompt_hook as ph

    # 环境守卫
    if shutil.which("bash") is None or shutil.which("curl") is None:
        pytest.skip("git-bash 或 curl 不可用，脚本自愈 E2E 无法实证")
    repo = Path(__file__).resolve().parent.parent
    ensure_script = repo / "scripts" / "ensure-graphify-server.sh"
    venv_bin = str(Path(sys.executable).parent)
    if not (ensure_script.exists() and Path(venv_bin, "graphify-mcp.exe").exists()):
        pytest.skip("ensure 脚本或 graphify-mcp 缺失，跳过自愈 E2E")

    root = _mini_proj(tmp_path)
    graph_path = root / "graphify-out" / "graph.json"
    graph_path.parent.mkdir(parents=True, exist_ok=True)
    graph_path.write_text(json.dumps({
        "directed": True,
        "nodes": [
            {"id": "foo", "label": "foo", "community": 0, "source_file": "a.py", "source_location": "L3"},
            {"id": "bar", "label": "bar", "community": 0, "source_file": "b.py", "source_location": "L1"},
        ],
        "links": [{"source": "foo", "target": "bar", "relation": "calls"}],
    }), encoding="utf-8")

    port = _free_port()
    env = dict(os.environ,
               PATH=venv_bin + os.pathsep + os.environ.get("PATH", ""),
               GRAPHIFY_MCP_PORT=str(port),
               GRAPHIFY_SERVE_PORT=str(port),
               GRAPHIFY_SERVE_LAUNCH_MARKER=str(tmp_path / "launch.marker"))
    monkeypatch.setenv("GRAPHIFY_MCP_PORT", str(port))
    monkeypatch.setenv("GRAPHIFY_SERVE_PORT", str(port))
    monkeypatch.setenv("GRAPHIFY_SERVE_LAUNCH_MARKER", str(tmp_path / "launch.marker"))
    # 本地回退哨兵：区分"本地"与"HTTP 恢复"
    monkeypatch.setattr(ph, "_query_locally", lambda prompt, graph_path: "LOCAL-FALLBACK")
    monkeypatch.setenv("GRAPHIFY_ALLOW_HTTP_MCP", "1")

    proc = subprocess.Popen(
        [sys.executable, "-m", "graphify.serve", str(graph_path),
         "--transport", "http", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(root), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    revived = None
    try:
        assert _wait_http(f"http://127.0.0.1:{port}/health"), "初始 server 未就绪"
        plan = {"graph_path": str(graph_path), "project_root": str(root)}

        # 首次 HTTP 查询成功（server 活着，真实 HTTP 结果）
        r0 = ph._query_graph(plan, "how does foo relate to bar")
        assert r0 and r0 != "LOCAL-FALLBACK", f"首次应走 HTTP（真实结果），got {r0!r}"

        # kill server → 首条 prompt：本地回退 + ensure-server 拉起一次
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
        time.sleep(0.3)  # 确保端口释放（Windows TIME_WAIT 不阻塞 bind 同进程）

        r1 = ph._query_graph(plan, "how does foo relate to bar")
        assert r1 == "LOCAL-FALLBACK", f"server 死后首条应本地回退，got {r1!r}"
        # ensure-server 拉起一次（防抖背书）：等新 server 就绪（同端口）
        assert _wait_http(f"http://127.0.0.1:{port}/health", timeout=25), \
            "ensure-server 应拉起新 server（/health 恢复）"
        # 记录新 server 进程以清理
        revived = True

        # 次条 prompt：恢复 HTTP（真实结果，非本地哨兵）
        r2 = ph._query_graph(plan, "how does foo relate to bar")
        assert r2 and r2 != "LOCAL-FALLBACK", f"次条应恢复 HTTP（真实结果），got {r2!r}"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
        # 清理 ensure-server 拉起的复活 server（nohup 脱离进程树，pytest 退出后仍存活——
        # 端口监听者按 netstat+taskkill 清理，避免孤儿进程占端口/锁 tmp_path）
        _kill_port(port)
