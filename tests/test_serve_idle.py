"""serve-memory 票 02：R3 serve idle 自杀——--idle-timeout 无流量优雅退出 + 停机协议。

spec: docs/specs/serve-memory-spec.md §R3（:55-69）与 §Testing 验收 5（:102）。

覆盖：
- 验收 5（进程面）：--idle-timeout 2 静默 → 进程退出（returncode 0，/health 不可达）
- 验收 5（停机协议面，确定性）：真实 uvicorn 线程 + 真实 watcher（--watch）——编辑源文件
  进防抖窗（debounce 拉长保 pending 未 flush），idle 触发 → 优雅退出 → lifespan finally →
  stop_all → final flush 落盘（graph.json 反映编辑，铁律 2）
- 单元：middleware 任何 HTTP 请求续命（touch）；非 http scope 不续命
- 单元：monitor 超时置 should_exit；活动内不触发
- 单元：0 禁用（idle_timeout<=0 不包 middleware 不启 monitor）
- 活动定义 = 任何 HTTP 请求（/query + /health 等）；注记未来外部监控时 /health 需排除续命
- --watch 关闭零 import 既有回归锁保持：serve_idle 仅 http + idle 启用时 import
"""
import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest


# === 测试辅助 ===

def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_http(url: str, timeout: float = 20.0, interval: float = 0.2) -> bool:
    """轮询直至 GET 200（/health 探活）。"""
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def _mini_proj(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "a.py").write_text(
        "import b\n\ndef foo():\n    return b.bar()\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("def bar():\n    return 1\n", encoding="utf-8")
    return tmp_path


def _write_graph(path: Path, nodes: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "directed": True,
        "nodes": [{"id": n, "label": n, "community": 0} for n in nodes],
        "links": [],
    }
    path.write_text(json.dumps(data), encoding="utf-8")


class _FakeServer:
    """monitor 的 server 接缝（uvicorn.Server 的最小替身：should_exit 属性）。"""

    def __init__(self):
        self.should_exit = False


class _FakeMonitor:
    """middleware 的 monitor 接缝：记录 touch 次数。"""

    def __init__(self):
        self.touched = 0

    def touch(self) -> None:
        self.touched += 1


async def _send_stub(*a, **k):
    """ASGI send 替身（echo app 发响应用，断言不关心响应体）。"""
    pass


async def _echo_app(scope, receive, send):
    """最小 ASGI app：返回 200（middleware 续命探针用）。"""
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def _http_scope(path: str = "/health") -> dict:
    return {
        "type": "http",
        "method": "GET",
        "path": path,
        "headers": [],
        "query_string": b"",
        "scheme": "http",
        "server": ("127.0.0.1", 8765),
        "client": ("127.0.0.1", 5555),
        "root_path": "",
    }


# === 单元：middleware 续命（活动定义 = 任何 HTTP 请求）====

def test_middleware_touches_on_http_request():
    """活动定义：任何 HTTP 请求（/health 等）都续命（touch monitor）。"""
    from graphify import serve_idle
    mon = _FakeMonitor()
    app = serve_idle.IdleTimeoutMiddleware(_echo_app, mon)
    asyncio.run(app(_http_scope(), None, _send_stub))
    assert mon.touched == 1, "HTTP 请求应续命一次"


def test_middleware_ignores_non_http_scope():
    """非 http scope（websocket 等）不续命。"""
    from graphify import serve_idle
    mon = _FakeMonitor()
    app = serve_idle.IdleTimeoutMiddleware(_echo_app, mon)
    scope = {"type": "websocket", "path": "/mcp", "headers": []}
    asyncio.run(app(scope, None, _send_stub))
    assert mon.touched == 0, "websocket scope 不应续命"


def test_middleware_touches_on_lifespan_startup():
    """lifespan.startup 续命一次：idle 时钟从 server 就绪起算（启动耗时不侵占静默窗口，
    防慢机 flake——启动 > idle_timeout 时 monitor 在 /health 就绪轮询前误退）。"""
    from graphify import serve_idle
    mon = _FakeMonitor()
    seen = []

    async def _lifespan_app(scope, receive, send):
        msg = await receive()
        seen.append(msg.get("type"))
        if msg.get("type") == "lifespan.startup":
            await send({"type": "lifespan.startup.complete"})

    async def _lifespan_receive():
        return {"type": "lifespan.startup"}

    async def _lifespan_send(msg):
        pass

    app = serve_idle.IdleTimeoutMiddleware(_lifespan_app, mon)
    scope = {"type": "lifespan"}
    asyncio.run(app(scope, _lifespan_receive, _lifespan_send))
    assert seen == ["lifespan.startup"], f"lifespan 消息未透传: {seen}"
    assert mon.touched == 1, "lifespan.startup 应续命一次（server 就绪即 idle 时钟起点）"


# === 单元：monitor 超时置 should_exit ===

def test_monitor_sets_should_exit_after_idle_timeout():
    """超时（距上次活动 ≥ idle_timeout）→ server.should_exit=True。"""
    from graphify import serve_idle
    server = _FakeServer()
    mon = serve_idle.IdleTimeoutMonitor(server, idle_timeout=0.05, poll_interval=0.05)
    mon.start()
    try:
        deadline = time.time() + 5
        while not server.should_exit and time.time() < deadline:
            time.sleep(0.02)
        assert server.should_exit is True, "idle 超时后 monitor 应置 should_exit"
    finally:
        mon._stop.set()


def test_monitor_does_not_fire_with_recent_activity():
    """持续活动（touch 续命）→ 不触发退出。"""
    from graphify import serve_idle
    server = _FakeServer()
    mon = serve_idle.IdleTimeoutMonitor(server, idle_timeout=0.15, poll_interval=0.03)
    mon.start()
    try:
        end = time.time() + 0.6
        while time.time() < end:
            mon.touch()
            time.sleep(0.02)
        assert server.should_exit is False, "持续活动不应触发 idle 退出"
    finally:
        mon._stop.set()


# === 单元：run_http 接线（0 禁用 + idle 启用包 middleware/启 monitor）====

def _install_fake_uvicorn(monkeypatch):
    """把 sys.modules['uvicorn'] 换成替身（Config/Server 可探针），隔离真实 uvicorn。"""
    import types
    captured = {"configs": [], "runs": 0}

    class FakeConfig:
        def __init__(self, app, **kwargs):
            self.app = app
            self.kwargs = kwargs
            captured["configs"].append(kwargs)

    class FakeServer:
        def __init__(self, config):
            self.config = config
            self.should_exit = False
            captured["server"] = self

        def run(self):
            captured["runs"] += 1

    fake = types.ModuleType("uvicorn")
    fake.Config = FakeConfig
    fake.Server = FakeServer
    monkeypatch.setitem(sys.modules, "uvicorn", fake)
    return captured


def test_run_http_zero_idle_disables_monitor(monkeypatch):
    """0 禁用：idle_timeout<=0 → 不包 middleware、不启 monitor、不注入优雅停机 Config
    （纯 run 等价，兑现 docstring 承诺）。"""
    from graphify import serve_idle
    captured = _install_fake_uvicorn(monkeypatch)
    serve_idle.run_http("APP", host="127.0.0.1", port=1234, idle_timeout=0)
    assert captured["runs"] == 1
    assert captured["server"].config.app == "APP", "0 禁用时 app 不应被 middleware 包裹"
    assert "timeout_graceful_shutdown" not in captured["configs"][0], \
        "0 禁用时不应注入 timeout_graceful_shutdown（与 uvicorn.run 等价）"


def test_run_http_idle_wraps_middleware_and_starts_monitor(monkeypatch):
    """idle_timeout>0 → app 外包 middleware + monitor 启动 + 优雅停机 Config（idle 需要）。"""
    from graphify import serve_idle
    captured = _install_fake_uvicorn(monkeypatch)
    serve_idle.run_http("APP", host="127.0.0.1", port=1234,
                        idle_timeout=1000, poll_interval=0.05)
    assert captured["runs"] == 1
    assert captured["configs"][0].get("timeout_graceful_shutdown") == 30, \
        "idle 启用时应注入 timeout_graceful_shutdown=30 安全带"
    wrapped = captured["server"].config.app
    assert isinstance(wrapped, serve_idle.IdleTimeoutMiddleware), \
        "idle 启用时 app 应被 IdleTimeoutMiddleware 包裹"
    mon = wrapped._monitor
    assert mon._idle_timeout == 1000


def test_run_http_poll_interval_defaults_from_env(monkeypatch):
    """poll_interval 缺省从 GRAPHIFY_IDLE_POLL_INTERVAL 取（测试旋钮，默认 60）。"""
    from graphify import serve_idle
    captured = _install_fake_uvicorn(monkeypatch)
    monkeypatch.setenv("GRAPHIFY_IDLE_POLL_INTERVAL", "0.25")
    serve_idle.run_http("APP", host="127.0.0.1", port=1234, idle_timeout=1000)
    assert captured["server"].config.app._monitor._poll_interval == 0.25
    assert serve_idle._env_poll_interval() == 0.25


def test_cli_idle_timeout_defaults_from_env(monkeypatch):
    """--idle-timeout 缺省从 GRAPHIFY_IDLE_TIMEOUT 取（同 --api-key 模式）；显式 CLI 赢过 env。"""
    import graphify.serve as S
    captured = {}
    monkeypatch.setattr(S, "serve", lambda gp: captured.setdefault("stdio", gp))
    monkeypatch.setattr(S, "serve_http", lambda gp, **k: captured.update(gp=gp, **k))
    monkeypatch.setenv("GRAPHIFY_IDLE_TIMEOUT", "123")
    S._main(["g.json", "--transport", "http"])
    assert captured["idle_timeout"] == 123, "缺省应从 GRAPHIFY_IDLE_TIMEOUT 取"
    S._main(["g.json", "--transport", "http", "--idle-timeout", "0"])
    assert captured["idle_timeout"] == 0, "显式 --idle-timeout 应赢过 env"


def test_idle_timeout_malformed_env_falls_back_3600(monkeypatch):
    """M4：GRAPHIFY_IDLE_TIMEOUT 畸形值回退 3600（不因 hook 侧 env 脏值崩启动）。"""
    import graphify.serve as S
    monkeypatch.setenv("GRAPHIFY_IDLE_TIMEOUT", "not-a-number")
    assert S._idle_timeout_default() == 3600.0
    monkeypatch.setenv("GRAPHIFY_IDLE_TIMEOUT", "-5")
    assert S._idle_timeout_default() == -5.0


# === 验收 5（进程面）：--idle-timeout 2 静默 → 进程退出 + 停机协议 ===

def test_cli_idle_timeout_exits_process(tmp_path):
    """验收 5（进程面）：--idle-timeout 2 静默 3s → 进程退出 + returncode 0 + /health 不可达。

    探活口径：/health 就绪轮询本身计入活动（续命），故静默窗口从就绪后起算——启动耗时
    不侵占静默窗口（启动慢也不误退，spec"idle 仅 http 生效"的进程面验证）。
    """
    root = _mini_proj(tmp_path)
    graph_path = root / "graphify-out" / "graph.json"
    _write_graph(graph_path, ["alpha"])
    port = _free_port()
    # PYTHONPATH 注入 <worktree 根>：子进程不经安装态也能 import graphify（editable 环境
    # 下无感；未安装环境下避免 ModuleNotFoundError → AssertionError 误报验收 5 失败）。
    src_root = str(Path(__file__).resolve().parent.parent)
    env = dict(os.environ, GRAPHIFY_IDLE_POLL_INTERVAL="0.1",
               PYTHONPATH=src_root + os.pathsep + os.environ.get("PYTHONPATH", ""))
    proc = subprocess.Popen(
        [sys.executable, "-m", "graphify.serve", str(graph_path),
         "--transport", "http", "--host", "127.0.0.1", "--port", str(port),
         "--idle-timeout", "2"],
        cwd=str(root), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        assert _wait_http(f"http://127.0.0.1:{port}/health", timeout=30), \
            "server 未就绪（/health 未通）"
        try:
            out, err = proc.communicate(timeout=25)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
            raise AssertionError(f"idle 超时后进程未退出；stderr:\n{err}")
        assert proc.returncode == 0, \
            f"进程应优雅退出 returncode=0，got {proc.returncode}；stderr:\n{err}"
        assert not _wait_http(f"http://127.0.0.1:{port}/health", timeout=2), \
            "进程退出后 /health 不应可达"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()


# === 验收 5（停机协议面，确定性）：idle 退出 → stop_all → final flush 落盘（铁律 2）====

def test_idle_exit_runs_stop_all_and_final_flush(tmp_path, monkeypatch):
    """验收 5（停机协议面）：真实 uvicorn 线程 + 真实 watcher（--watch）——编辑源文件进
    防抖窗（DEFAULT_DEBOUNCE 拉长保 pending 批次未 flush），idle 触发 → should_exit →
    优雅退出 → lifespan finally（stop_graphify_watcher）→ stop_all → final flush 落盘
    （graph.json 反映编辑内容，铁律 2：退出前 pending 批次完整 flush）。
    """
    pytest.importorskip("uvicorn")
    import graphify.serve as S
    import graphify.serve_watcher as W
    # 常量注入（registry 构造时读模块常量）：短轮询保编辑被快速检测；长防抖保 pending 批次
    # 在 idle 触发前不会自然 flush——final flush 是唯一落盘路径（确定性判别）。
    monkeypatch.setattr(W, "DEFAULT_POLL_INTERVAL", 0.2)
    monkeypatch.setattr(W, "DEFAULT_DEBOUNCE", 600.0)
    monkeypatch.setenv("GRAPHIFY_WATCH", "1")

    from graphify import serve_idle
    root = _mini_proj(tmp_path)
    graph_path = root / "graphify-out" / "graph.json"
    _write_graph(graph_path, ["alpha"])
    app = S._build_http_app(str(graph_path), json_response=True)
    port = _free_port()
    server_thread = threading.Thread(
        target=lambda: serve_idle.run_http(
            app, host="127.0.0.1", port=port, idle_timeout=8.0, poll_interval=0.2),
        daemon=True,
    )
    server_thread.start()
    try:
        assert _wait_http(f"http://127.0.0.1:{port}/health", timeout=30), "server 未就绪"
        # 编辑源文件 → watcher 短轮询检测 → pending changed 批次（长防抖内不 flush）
        (root / "a.py").write_text(
            "import b\n\ndef foo():\n    return b.bar()\n\ndef alpha_new():\n    return 1\n",
            encoding="utf-8",
        )
        time.sleep(1.0)  # 给 watcher 轮询一个检测周期
        server_thread.join(timeout=40)
        assert not server_thread.is_alive(), "idle 超时后 server 线程应退出"
    finally:
        if server_thread.is_alive():
            # 兜底：等退出（不应走到）；线程是 daemon，不 join 也不阻塞测试退出
            server_thread.join(timeout=5)
    # 铁律 2：退出前 pending 批次完整 flush → graph.json 反映编辑（alpha_new 节点落盘）。
    # 提取器对函数定义产 label="alpha_new()"（带括号），断言用该规范形态。
    data = json.loads(graph_path.read_text(encoding="utf-8"))
    labels = {n["label"] for n in data.get("nodes", [])}
    assert "alpha_new()" in labels, \
        f"final flush 未落盘编辑（graph.json 缺 alpha_new()）：labels={sorted(labels)}"
