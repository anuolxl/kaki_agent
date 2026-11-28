"""Loopback-only UI transport. It never exposes model credentials or arbitrary RPC methods."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import secrets
import socket
from pathlib import Path

from aiohttp import WSMsgType, web

from kaki import __version__
from kaki.app_controller import AppController
from kaki.config import safe_error
from kaki.types import KakiError

UI = Path(__file__).with_name("ui")


class AppServer:
    # 只监听 loopback 的 UI 传输层；HTTP/WS 不直接暴露任意 Shell 或模型凭据。
    def __init__(self, controller: AppController, *, port=0, initial_path=None, resume=None):
        self.controller = controller
        self.port, self.initial_path, self.resume = port, initial_path, resume
        # 本机 UI capability 随进程随机生成，与模型 API Key 完全分开。
        self.secret = secrets.token_urlsafe(32)
        self.origin = ""
        self.cookie = ""
        # 维护实时连接以便关闭广播与资源释放；连接断开不是任务终态。
        self.sockets = set()
        # 由退出 API/原生信号唤醒服务所有者，统一关闭链路。
        self.stop_event = asyncio.Event()
        self.runner = None
        self.initial = None
        self.initial_error = None
        self.on_focus = None
        self.closing = False

    @property
    def connect_url(self):
        # 一次性连接入口包含本地 capability；页面交换 Cookie 后会离开带票据 URL。
        return self.origin + "/connect/" + self.secret

    def authenticated(self, request):
        # 启动器用 Bearer、界面用 Cookie；二者都校验同一随机本地 capability。
        bearer = request.headers.get("Authorization", "")
        token = bearer[7:] if bearer.startswith("Bearer ") else request.cookies.get(self.cookie, "")
        return bool(token) and hmac.compare_digest(token, self.secret)

    @web.middleware
    async def guard(self, request, handler):
        # 统一 Host/Origin/认证与关闭门；本机端口也必须防跨站请求和 DNS rebinding。
        if request.host != self.origin.removeprefix("http://"):
            raise web.HTTPForbidden(text="Invalid local host")
        source = request.headers.get("Origin")
        if source and source != self.origin:
            raise web.HTTPForbidden(text="Cross-origin access denied")
        if request.path.startswith("/api/"):
            if not self.authenticated(request):
                raise web.HTTPUnauthorized(text="请从Kaki启动器打开此窗口")
            if request.method not in ("GET", "HEAD") and not request.headers.get("Authorization"):
                if source != self.origin or request.headers.get("X-Kaki-Client") != "1":
                    raise web.HTTPForbidden(text="Invalid App request origin")
            if self.closing and request.path != "/api/state":
                raise web.HTTPServiceUnavailable(text="Kaki正在退出")
        try:
            result = await handler(request)
        except KakiError as exc:
            result = web.json_response({"error": safe_error(exc)}, status=409)
        except (KeyError, ValueError, TypeError) as exc:
            result = web.json_response({"error": "请求参数无效：" + safe_error(exc)}, status=400)
        if isinstance(result, web.StreamResponse):
            result.headers["Cache-Control"] = "no-store"
            result.headers["X-Content-Type-Options"] = "nosniff"
            result.headers["Referrer-Policy"] = "no-referrer"
            result.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'"
            )
        return result

    async def start(self):
        # 注册有限路由并监听随机本地端口，再打开初始项目；初始路径错误保留在欢迎页诊断。
        application = web.Application(middlewares=[self.guard], client_max_size=512 * 1024)
        application.add_routes(
            [
                web.get("/", self.index),
                web.get("/assets/{name}", self.asset),
                web.get("/connect/{ticket}", self.connect),
                web.get("/health", self.health),
                web.get("/api/state", self.state),
                web.get("/api/events", self.events),
                web.post("/api/workspaces", self.open_workspace),
                web.get("/api/workspaces/{workspace}/sessions", self.sessions),
                web.post("/api/workspaces/{workspace}/sessions", self.new_session),
                web.get("/api/workspaces/{workspace}/sessions/{session}", self.detail),
                web.patch("/api/workspaces/{workspace}/sessions/{session}", self.edit_session),
                web.post("/api/workspaces/{workspace}/sessions/{session}/send", self.send),
                web.post("/api/workspaces/{workspace}/sessions/{session}/stop", self.stop),
                web.post("/api/workspaces/{workspace}/sessions/{session}/answer", self.answer),
                web.post("/api/shutdown", self.shutdown),
                web.post("/api/focus", self.focus),
            ]
        )
        self.runner = web.AppRunner(application, access_log=None, shutdown_timeout=3)
        await self.runner.setup()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", self.port))
        sock.listen(32)
        sock.setblocking(False)
        self.port = sock.getsockname()[1]
        self.origin = f"http://127.0.0.1:{self.port}"
        self.cookie = f"kaki_app_{self.port}"
        await web.SockSite(self.runner, sock).start()
        if self.initial_path:
            try:
                engine = await self.controller.open_workspace(str(self.initial_path))
                if self.resume and self.resume != "last":
                    rows = await self.controller.sessions(engine.runtime.workspace.id)
                    matches = [s for s in rows if s["id"] == self.resume or s["id"].startswith(self.resume)]
                    if len(matches) != 1:
                        raise KakiError("会话不存在或前缀不唯一")
                    engine.session = matches[0]["id"]
                self.initial = {"workspace_id": engine.runtime.workspace.id, "session_id": engine.session}
            except Exception as exc:
                self.initial_error = safe_error(exc)
        return self

    async def index(self, _):
        # 只提供内置页面壳，模型正文不会作为可执行页面脚本返回。
        return web.Response(text=(UI / "index.html").read_text(), content_type="text/html")

    async def asset(self, request):
        # 静态资源按白名单提供，不能把工作区任意文件当网页文件服务器。
        name = request.match_info["name"]
        if name not in {"app.mjs", "state.mjs", "app.css", "icon.svg"}:
            raise web.HTTPNotFound()
        mime = (
            "text/javascript"
            if name.endswith(".mjs")
            else "text/css"
            if name.endswith(".css")
            else "image/svg+xml"
        )
        return web.Response(body=(UI / name).read_bytes(), content_type=mime)

    async def connect(self, request):
        # 把短暂 URL 票据换成 HttpOnly Cookie，再重定向到不含票据的根页。
        if not hmac.compare_digest(request.match_info["ticket"], self.secret):
            raise web.HTTPForbidden()
        result = web.Response(status=303, headers={"Location": "/"})
        result.set_cookie(self.cookie, self.secret, httponly=True, samesite="Strict", path="/")
        return result

    async def health(self, _):
        # 健康检查只公布服务状态与版本，不公布工作区路径或认证数据。
        return web.json_response({"app": "kaki", "version": __version__, "closing": self.closing})

    async def state(self, _):
        # 导航状态接口；projects 不是所有 Runtime 的实时执行详情。
        return web.json_response(
            {
                "projects": self.controller.projects(),
                "initial": self.initial,
                "initial_error": self.initial_error,
                "version": __version__,
                "model": self.controller.config.agents.defaults.model,
                "closing": self.closing,
                "seq": self.controller.seq,
            }
        )

    async def open_workspace(self, request):
        # HTTP 项目入口只委托 Controller，不在传输层维护第二套会话/模型状态。
        data = await request.json()
        e = await self.controller.open_workspace(data["path"])
        return web.json_response(
            {
                "workspace_id": e.runtime.workspace.id,
                "path": str(e.runtime.workspace.root),
                "session_id": e.session,
            }
        )

    async def sessions(self, request):
        # 传输层仅解析归档选项；查询与项目归属由 Controller/Store 负责。
        rows = await self.controller.sessions(
            request.match_info["workspace"], request.query.get("archived") == "1"
        )
        return web.json_response({"sessions": rows})

    async def new_session(self, request):
        # 新建会话属于导航操作，不直接启动模型或工具。
        data = await request.json()
        identity = await self.controller.new_session(
            request.match_info["workspace"], data.get("title", "新会话")
        )
        return web.json_response({"session_id": identity})

    async def detail(self, request):
        # 返回 Controller 合成的历史与 live，供刷新/重连核对，不凭前端缓存推断终态。
        return web.json_response(
            await self.controller.detail(request.match_info["workspace"], request.match_info["session"])
        )

    async def edit_session(self, request):
        # 只允许标题/归档两类会话管理字段，禁止任意写数据库字段。
        data = await request.json()
        workspace, session = request.match_info["workspace"], request.match_info["session"]
        if "title" in data:
            await self.controller.rename(workspace, session, data["title"])
        if "archived" in data:
            if type(data["archived"]) is not bool:
                raise ValueError("archived需要布尔值")
            await self.controller.archive(workspace, session, data["archived"])
        return web.json_response({"ok": True})

    async def send(self, request):
        # 提交请求后立即返回 accepted；真正完成通过事件与 detail 中的持久状态确认。
        data = await request.json()
        await self.controller.send(
            request.match_info["workspace"],
            request.match_info["session"],
            data["text"],
            data.get("request_id"),
        )
        return web.json_response({"accepted": True}, status=202)

    async def stop(self, request):
        # 明确停止当前会话的本轮工作，窗口和其他项目保持可用。
        await self.controller.stop(request.match_info["workspace"], request.match_info["session"])
        return web.json_response({"ok": True})

    async def answer(self, request):
        # 请求体只传问题 ID、明确回答和可选 choice；授权有效性在内核服务复核。
        data = await request.json()
        await self.controller.answer(
            request.match_info["workspace"],
            request.match_info["session"],
            data["question_id"],
            data["text"],
            data.get("choice"),
        )
        return web.json_response({"ok": True})

    async def shutdown(self, _):
        # 先发布正在关闭，再唤醒服务所有者收尾；不直接强杀 Python 进程。
        self.closing = True
        self.controller.publish("app.closing")
        asyncio.get_running_loop().call_later(0.15, self.stop_event.set)
        return web.json_response({"ok": True})

    async def focus(self, request):
        # 启动器复用现有窗口时走这里；打开项目不等于给该项目发新任务。
        data = await request.json()
        if data.get("path"):
            e = await self.controller.open_workspace(data["path"])
            self.controller.publish("navigate", workspace_id=e.runtime.workspace.id, session_id=e.session)
        if self.on_focus:
            self.on_focus()
        return web.json_response({"ok": True})

    async def events(self, request):
        # 每连接独立队列/写任务；断线与丢包需要 resync，不应让慢 UI 阻塞执行内核。
        if request.headers.get("Origin") != self.origin:
            raise web.HTTPForbidden()
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=1024)
        await ws.prepare(request)
        queue = asyncio.Queue(maxsize=256)
        self.controller.subscribers.add(queue)
        self.sockets.add(ws)

        async def writer():
            await ws.send_json({"type": "connected", "seq": self.controller.seq})
            while True:
                message = await queue.get()
                await asyncio.wait_for(ws.send_str(json.dumps(message, ensure_ascii=False)), 5)

        task = asyncio.create_task(writer(), name="kaki-app-viewer")
        try:
            async for message in ws:
                if message.type == WSMsgType.ERROR:
                    break
        finally:
            self.controller.subscribers.discard(queue)
            self.sockets.discard(ws)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, ConnectionError, TimeoutError, RuntimeError):
                await task
        return ws

    async def close(self):
        # 停止接单并通知客户端，再关闭订阅写任务、项目内核和 HTTP 资源。
        self.closing = True
        try:
            await asyncio.wait_for(self.controller.close(), 15)
        finally:
            for ws in tuple(self.sockets):
                await ws.close(code=1001, message=b"Kaki App stopped")
            if self.runner:
                await self.runner.cleanup()
