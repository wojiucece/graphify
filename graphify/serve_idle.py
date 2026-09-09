"""serve 内存优化 R3：idle 自杀监视器——ASGI middleware + daemon timer + uvicorn 包装。

独立模块分层（遵循 fts_cache.py / serve_watcher.py 先例）：serve.py 只做 import + 接线，
idle 监视的全部逻辑在此。仅 http transport 生效（stdio 随 stdin 退出，零影响）。

闭环语义：server 存活 ⇔ hysteresis 窗口内有 prompt 流过。超时（默认 3600s）无任何 HTTP
请求即置 ``server.should_exit=True`` → uvicorn 优雅退出（timeout_graceful_shutdown=30
安全带）→ lifespan finally（stop_graphify_watcher）→ stop_all → final flush（铁律 2：
退出前 pending 批次完整 flush）→ 内存按空闲段归零。退出后 prompt-hook 本地回退 +
ensure-server 非阻塞拉起，下一条 prompt 恢复毫秒级 HTTP。

活动定义 = 任何 HTTP 请求（/query + /health 等）。注记：本地单用户无外部监控，/health
计入续命可接受；未来接入外部监控时 /health 需排除续命。

测试旋钮：``GRAPHIFY_IDLE_POLL_INTERVAL``（默认 60）——生产按 spec"每 60s 检查"，
测试可注入短间隔快速验证 idle 触发。
"""
import os
import threading
import time


def _env_poll_interval() -> float:
    """测试旋钮：GRAPHIFY_IDLE_POLL_INTERVAL（默认 60s，spec"每 60s 检查"）。"""
    try:
        return max(0.05, float(os.environ.get("GRAPHIFY_IDLE_POLL_INTERVAL", "60")))
    except ValueError:
        return 60.0


class IdleTimeoutMonitor:
    """daemon 线程：超时置 ``server.should_exit=True`` → uvicorn 优雅退出。

    middleware 每次 HTTP 请求 ``touch()`` 续命；本线程每 ``poll_interval`` 检查一次，
    距上次活动 ≥ ``idle_timeout`` 即触发退出。``should_exit`` 是 uvicorn.Server 的标准
    退出信号（主循环周期性检查），优雅停机 + lifespan finally 完成 final flush。
    """

    def __init__(self, server, idle_timeout: float, poll_interval: float = 60.0):
        self._server = server
        self._idle_timeout = idle_timeout
        self._poll_interval = poll_interval
        self._last_activity = time.monotonic()
        self._stop = threading.Event()

    def touch(self) -> None:
        """任何 HTTP 请求续命（middleware 调用）。"""
        self._last_activity = time.monotonic()

    def start(self) -> None:
        t = threading.Thread(target=self._run, name="serve-idle-monitor", daemon=True)
        t.start()

    def _run(self) -> None:
        while not self._stop.wait(self._poll_interval):
            if time.monotonic() - self._last_activity >= self._idle_timeout:
                self._server.should_exit = True
                return


class IdleTimeoutMiddleware:
    """ASGI middleware：任何 HTTP 请求续命（记录 last-activity）。

    原始 ASGI 实现（非 BaseHTTPMiddleware，避免缓冲 SSE 流）——与 _ApiKeyMiddleware
    同哲学，覆盖 /mcp /query /health 全部 HTTP 请求。
    """

    def __init__(self, app, monitor: IdleTimeoutMonitor):
        self.app = app
        self._monitor = monitor

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            self._monitor.touch()
        await self.app(scope, receive, send)


def run_http(app, *, host: str, port: int, idle_timeout: float = 0.0,
             poll_interval: float | None = None) -> None:
    """uvicorn.run 的替代：Config+Server 持句柄，idle 超时优雅退出。

    - ``idle_timeout`` <= 0（默认 0）禁用 idle 监视（纯 run，行为与 uvicorn.run 等价，
      不注入任何额外 Config 默认）。
    - ``idle_timeout`` > 0 时：app 外包 idle middleware（任何请求续命）+ daemon 监视器
      启动，超时置 ``server.should_exit=True`` → uvicorn 优雅退出
      （``timeout_graceful_shutdown=30`` 安全带，仅 idle 启用时设置）→ lifespan finally
      → stop_all → final flush（铁律 2）。
    - ``poll_interval`` 缺省从 GRAPHIFY_IDLE_POLL_INTERVAL 取（默认 60s，测试旋钮）。
    """
    import uvicorn
    config_kwargs: dict = {"host": host, "port": port}
    idle_on = bool(idle_timeout and idle_timeout > 0)
    if idle_on:
        # 优雅停机安全带仅 idle 退出需要（idle_timeout=0 保持 uvicorn.run 完全等价）。
        config_kwargs["timeout_graceful_shutdown"] = 30
    server = uvicorn.Server(uvicorn.Config(app, **config_kwargs))
    if idle_on:
        monitor = IdleTimeoutMonitor(
            server,
            idle_timeout,
            poll_interval if poll_interval is not None else _env_poll_interval(),
        )
        # app 外包 middleware（最外层，覆盖 /mcp /query /health 全部 HTTP 请求续命）
        server.config.app = IdleTimeoutMiddleware(app, monitor)
        monitor.start()
    server.run()
