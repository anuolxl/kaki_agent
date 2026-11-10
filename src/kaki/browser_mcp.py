"""A dedicated MCP owner task keeps anyio cancel scopes in their entering task."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
from datetime import timedelta

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client

from kaki.config import Config, safe_error
from kaki.types import KakiError, ToolResult
from kaki.workspace import Workspace

PLAYWRIGHT_VERSION = "0.0.85"


class BrowserMcp:
    def __init__(self, workspace: Workspace, config: Config):
        self.workspace, self.config = workspace, config
        self.option = config.tools.mcp_servers.get("browser")
        self.queue = asyncio.Queue()
        self.task = None
        self.ready = None
        self.tools = []
        self.error = None

    @property
    def enabled(self):
        return self.option is not None

    async def start(self):
        if not self.enabled:
            return []
        if self.task is None:
            self.ready = asyncio.get_running_loop().create_future()
            self.task = asyncio.create_task(self._serve(), name="kaki-browser-mcp")
        return await asyncio.wait_for(asyncio.shield(self.ready), 90)

    async def _serve(self):
        opt = self.option
        args = list(opt.args or ["-y", f"@playwright/mcp@{PLAYWRIGHT_VERSION}", "--browser", "chrome"])
        if "--isolated" not in args and not any(x.startswith("--user-data-dir") for x in args):
            args += ["--user-data-dir", str(self.workspace.state / "browser-profile")]
        if not any(x.startswith("--output-dir") for x in args):
            args += ["--output-dir", str(self.workspace.state / "browser-output")]
        server = StdioServerParameters(
            command=opt.command, args=args, env=opt.env or None, cwd=self.workspace.root
        )

        async def roots(_):
            return types.ListRootsResult(
                roots=[types.Root(uri=self.workspace.root.as_uri(), name=self.workspace.root.name)]
            )

        log = (self.workspace.state / "browser-mcp.log").open("a")
        try:
            async with stdio_client(server, errlog=log) as (read, write):
                async with ClientSession(
                    read,
                    write,
                    read_timeout_seconds=timedelta(seconds=opt.tool_timeout),
                    list_roots_callback=roots,
                    client_info=types.Implementation(name="kaki-agent", version="0.1.0"),
                ) as session:
                    await session.initialize()
                    listing = await session.list_tools()
                    self.tools = []
                    for t in listing.tools:
                        original = t.name
                        alias = "kaki_browser_" + original.removeprefix("browser_")
                        self.tools.append(
                            {
                                "name": alias,
                                "original": original,
                                "description": t.description or original,
                                "parameters": t.inputSchema or {"type": "object", "properties": {}},
                            }
                        )
                    self.ready.set_result(self.tools)
                    while True:
                        item = await self.queue.get()
                        if item is None:
                            break
                        name, arguments, future = item
                        if future.cancelled():
                            continue
                        operation = asyncio.create_task(session.call_tool(name, arguments=arguments))
                        future.add_done_callback(
                            lambda f, op=operation: op.cancel() if f.cancelled() and not op.done() else None
                        )
                        try:
                            result = await asyncio.wait_for(operation, opt.tool_timeout)
                            text = []
                            artifacts = []
                            parts = []
                            for block in result.content:
                                if isinstance(block, types.TextContent):
                                    text.append(block.text)
                                elif isinstance(block, types.ImageContent):
                                    raw = base64.b64decode(block.data, validate=True)
                                    suffix = ".png" if block.mimeType == "image/png" else ".jpg"
                                    a = self.workspace.artifact(raw, media_type=block.mimeType, suffix=suffix)
                                    artifacts.append(a)
                                    parts.append(
                                        {
                                            "type": "input_image",
                                            "artifact_id": a["artifact_id"],
                                            "media_type": block.mimeType,
                                        }
                                    )
                                    text.append("[image artifact: " + a["artifact_id"] + "]")
                                else:
                                    text.append(block.model_dump_json())
                            if getattr(result, "structuredContent", None):
                                text.append(json.dumps(result.structuredContent, ensure_ascii=False))
                            output = ToolResult(
                                "\n".join(text) or "(no output)",
                                failed=bool(result.isError),
                                artifacts=artifacts,
                                parts=parts,
                            )
                            if not future.done():
                                future.set_result(output)
                        except asyncio.CancelledError:
                            if asyncio.current_task().cancelling():
                                raise
                            if not future.done():
                                future.set_exception(KakiError("浏览器调用被取消，需核对页面现场"))
                        except Exception as exc:
                            if not future.done():
                                future.set_result(
                                    ToolResult("浏览器MCP执行失败：" + safe_error(exc), failed=True)
                                )
        except BaseException as exc:
            self.error = safe_error(exc)
            if not self.ready.done():
                self.ready.set_exception(KakiError("浏览器MCP启动失败：" + self.error))
            while not self.queue.empty():
                item = self.queue.get_nowait()
                if item is not None and not item[2].done():
                    item[2].set_exception(KakiError("浏览器MCP已退出"))
            if isinstance(exc, asyncio.CancelledError):
                raise
        finally:
            log.close()

    async def call(self, name: str, arguments: dict) -> ToolResult:
        await self.start()
        if self.task.done():
            raise KakiError("浏览器MCP已退出；请重新启动Kaki后重试")
        original = next((t["original"] for t in self.tools if t["name"] == name), None)
        if not original:
            raise KakiError("未注册的浏览器工具")
        if "files" in arguments and isinstance(arguments["files"], list):
            arguments = {**arguments, "files": [str(self.workspace.path(p)) for p in arguments["files"]]}
        future = asyncio.get_running_loop().create_future()
        await self.queue.put((original, arguments, future))
        return await future

    async def close(self):
        if self.task and not self.task.done():
            await self.queue.put(None)
            try:
                await asyncio.wait_for(asyncio.shield(self.task), 8)
            except (TimeoutError, asyncio.CancelledError):
                self.task.cancel()
                with contextlib.suppress(BaseException):
                    await self.task
        if self.task:
            with contextlib.suppress(BaseException):
                self.task.result()
