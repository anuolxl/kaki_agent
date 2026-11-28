"""Local application owner: one Turn, one workspace lease, one terminal event."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import json
import re

from PIL import Image

from kaki.browser_mcp import BrowserMcp
from kaki.checkpoint import Checkpoint
from kaki.config import Config, safe_error
from kaki.context import ContextEngine
from kaki.decisions import DecisionService
from kaki.loop import AgentLoop, verification_evidence, verification_status
from kaki.permissions import PermissionService
from kaki.provider import RightCodeProvider
from kaki.recovery import Recovery
from kaki.store import Store
from kaki.tools import Tool, ToolRegistry, integer, string
from kaki.types import Event, InteractionRequired, KakiError, ToolCall, ToolResult, TurnResult
from kaki.understanding import UnderstandingService
from kaki.workspace import Workspace, WorkspaceLease


class Runtime:
    # 单个 Git 工作区的应用内核；拥有会话、执行租约和资源生命周期，UI 只订阅它的事件。
    def __init__(self, workspace: Workspace, config: Config, *, provider=None, interactive: bool = True):
        # 先构造资源所有者与空状态；模型、窗口上下文和工具注册表在 initialize 中准备。
        self.workspace, self.config = workspace, config
        self.interactive = interactive
        # SQLite 事实存储归本 Runtime 所有，连接在它自己的唯一 worker 线程中使用。
        self.store = Store(workspace.state / "kaki.sqlite3", workspace.id)
        self.provider = provider
        self.browser = BrowserMcp(workspace, config)
        # 依赖需要异步 initialize 后才可用，构造 Runtime 不等于已经组装窗口。
        self.context = None
        self.tools = None
        self.checkpoint = Checkpoint(workspace, self.store, config)
        self.recovery = Recovery(self.store)
        self.lease = WorkspaceLease(workspace.state)
        self.session_id = ""
        # 当前 Turn 的 asyncio Task；协程任务不是线程，也不是跨进程锁。
        self.active = None
        # 真正 accept_turn 后才有 t_ ID，准备期可能为空。
        self.active_turn = ""
        # 流式展示草稿；完整模型输出仍要单独事务提交。
        self.partial_text = ""
        # 同进程观察者负责 UI 投影，不替代事务内持久事件。
        self.listeners = []
        # 唯一待回答交互，内部 Future 不写数据库也不暴露给 HTTP DTO。
        self.question = None
        # submit 的就绪门，不等于已经创建 Turn 或调用主模型。
        self.initialized = False
        # 关闭标志使退出流程不再发起耗时的 after 快照；普通停止仍正常收尾。
        self.closed = False
        # 恢复事实只注入下一轮核验，不自动重发旧动作。
        self.recovery_plan = {}
        # 可选浏览器失败的可见诊断，不把它扩散成所有本地工具不可用。
        self.browser_warning = None
        self._browser_attempted = False
        self.decisions = DecisionService(self)
        self.permissions = PermissionService(self)
        self.understanding = None

    async def emit(self, kind: str, turn: str = "", data: dict | None = None):
        # 同进程进度分发；并非每个 UI 事件都落库，持久事实由 Store 事务单独记录。
        event = Event(kind, turn, data or {})
        for listener in self.listeners:
            await listener(event)

    def image_part(self, part: dict) -> dict:
        # 发送模型前才解析受控 artifact 图片；窗口估算使用图片预留，避免把 base64 当文本预算。
        path = self.workspace.artifact_path(part["artifact_id"])
        image = Image.open(path)
        image.thumbnail((1568, 1568))
        buffer = io.BytesIO()
        image.convert("RGB").save(buffer, format="PNG")
        return {
            "type": "input_image",
            "image_url": "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode(),
            "detail": "auto",
        }

    async def initialize(self, resume: str | None = None, *, connect_browser: bool = True):
        # 初始化/恢复会话及内核依赖；恢复只协调日志状态，不自动执行旧任务或请求主模型。
        if self.initialized:
            return
        await self.store.open()
        if self.provider is None:
            self.provider = RightCodeProvider(
                self.config, lambda record: self.store.run("call_record", record), self.image_part
            )
        self.context = ContextEngine(self.workspace, self.store, self.config, self.provider)
        self.understanding = UnderstandingService(self.config, self.provider)
        # 注册真实能力并把依赖检查回调接到 Skill 目录；口头“开放”不会改变工具表。
        self.tools = ToolRegistry(
            self.workspace, self.config, self.context, self.browser, self.ask_user, self.permissions
        )
        self.context.skills.available_tools = self.tools.available_names
        self.context.skills.available_commands = self.tools.development.available_commands
        self.tools.add(
            Tool(
                "kaki_artifact_read",
                "Read full stored text tool output by artifact_id with exact character pagination.",
                {"artifact_id": string(), "offset": integer(), "limit": integer("characters", 100, 16000)},
                ["artifact_id"],
                "READ",
                self.tools.artifact_read,
                True,
            )
        )
        # 只选择当前项目中的会话；恢复既有 Session 需要模型兼容与租约协调。
        sessions = await self.store.run("sessions")
        if resume:
            if resume == "last":
                if not sessions:
                    raise KakiError("当前项目还没有可恢复会话")
                self.session_id = sessions[0]["id"]
            else:
                matches = [s for s in sessions if s["id"] == resume or s["id"].startswith(resume)]
                if len(matches) != 1:
                    raise KakiError("会话不存在或前缀不唯一")
                self.session_id = matches[0]["id"]
            await self.check_session_model(self.session_id)
            try:
                self.lease.acquire()
            except KakiError:
                self.recovery_plan = {"items": [], "notice": "工作区仍有其他活跃任务，未执行恢复改写"}
            else:
                try:
                    self.recovery_plan = await self.recovery.reconcile(self.session_id)
                finally:
                    self.lease.release()
        else:
            self.session_id = await self.store.run("new_session")
        self.context.last_metadata = (await self.store.run("context_get", self.session_id))["plan"]
        if connect_browser:
            await self.ensure_browser()
        self.initialized = True
        await self.emit(
            "runtime.ready",
            data={
                "session_id": self.session_id,
                "browser_tools": len(self.browser.tools),
                "browser_warning": self.browser_warning,
                "recovery": self.recovery_plan,
            },
        )

    async def ensure_browser(self):
        # 可选 MCP 惰性连接；连接失败记录警告，让本地代码能力仍能工作。
        """App history opens immediately; MCP initialization is deferred until task execution."""
        if not self.browser.enabled or self._browser_attempted:
            return
        self._browser_attempted = True
        await self.emit("runtime.status", data={"text": "正在连接Playwright MCP…"})
        try:
            await self.tools.connect_browser()
        except asyncio.CancelledError:
            self._browser_attempted = False
            raise
        except Exception as exc:
            self.browser_warning = safe_error(exc)
            await self.emit("runtime.warning", data={"text": "浏览器未连接：" + self.browser_warning})

    async def new_session(self, title="新会话"):
        # 新会话改变后续任务的历史作用域；活跃 Turn 结束前不能切走执行对象。
        if self.active and not self.active.done():
            raise KakiError("请先取消当前任务")
        self.session_id = await self.store.run("new_session", title)
        self.recovery_plan = {}
        return self.session_id

    async def switch_session(self, identity: str):
        # 先校验模型与租约，再协调恢复事实并载入窗口元数据；不会重放 UNKNOWN 副作用。
        if self.active and not self.active.done():
            raise KakiError("请先结束或取消当前任务")
        sessions = await self.store.run("sessions")
        matches = [s for s in sessions if s["id"] == identity or s["id"].startswith(identity)]
        if identity == "last" and sessions:
            matches = [sessions[0]]
        if len(matches) != 1:
            raise KakiError("会话不存在或前缀不唯一")
        await self.check_session_model(matches[0]["id"])
        self.lease.acquire()
        try:
            self.session_id = matches[0]["id"]
            self.recovery_plan = await self.recovery.reconcile(self.session_id)
            self.context.last_metadata = (await self.store.run("context_get", self.session_id))["plan"]
        finally:
            self.lease.release()
        return self.session_id

    async def check_session_model(self, session: str):
        # 原生 reasoning 与模型相关；禁止把旧会话静默切到另一个主模型重放。
        models = await self.store.run("session_models", session)
        current = self.config.agents.defaults.model.removeprefix("openai/")
        if any(model.removeprefix("openai/") != current for model in models):
            raise KakiError(
                "该会话的模型与当前配置不同，原生reasoning项不能保证跨模型重放；请使用原模型恢复或新建会话"
            )

    @property
    def snapshots_enabled(self):
        # interactive 指有交互界面的自动快照策略，不是每次写入都弹一个快照确认框。
        policy = self.config.runtime.checkpoint.policy
        return policy == "always" or policy == "interactive" and self.interactive

    def submit(self, text: str, request_id: str | None = None) -> asyncio.Task:
        # 接单并创建一个 asyncio Task；返回的是任务句柄，不是另起线程或另一个主循环。
        if not self.initialized:
            raise KakiError("Runtime还未就绪")
        if self.active and not self.active.done():
            raise KakiError("当前任务仍在运行；可以取消或回答待确认问题")
        if not text.strip():
            raise KakiError("任务不能为空")
        self.active = asyncio.create_task(self._turn(text, request_id), name="kaki-user-turn")
        return self.active

    async def run(self, text: str, request_id: str | None = None) -> TurnResult:
        # 便捷的 await submit：等待该 Turn 完整收尾，供 App、测试及单次命令复用。
        return await self.submit(text, request_id)

    async def _turn(self, text: str, request_id: str | None) -> TurnResult:
        # 一轮请求的唯一生命周期边界：接收/幂等 → 理解与澄清 → AgentLoop → 快照/统计/终态。
        turn = ""
        result = None
        user = None
        warnings = []
        # 首次获准变更前才捕获；已有副作用不会因取消而隐式撤回。
        before = None
        after = None
        self.partial_text = ""
        self.permissions.begin()
        self.tools.begin_turn()
        self.decisions.answered.clear()

        async def before_mutation():
            # 首次获准文件写入或测试前才建立基线；普通对话无需扫描整个工作树。
            nonlocal before
            if before is not None or not self.snapshots_enabled:
                return
            await self.emit("runtime.status", turn, {"text": "建立工作区基线快照…"})
            snapshot = await self.checkpoint.capture(turn, "before")
            before = snapshot["id"]
            if snapshot["state"] != "ready":
                warnings.append(snapshot.get("error", "快照失败"))
                await self.emit("snapshot.warning", turn, snapshot)

        # 操作系统租约跨进程保护同一工作树，弥补单 Runtime 的 busy 检查。
        # 1. 跨进程取得工作树执行权；只有 UI busy 检查不足以阻止另一个终端同时修改。
        self.lease.acquire()
        try:
            # 2. 接收请求与原始输入同事务提交；重复 request_id 返回旧结果，不创建新副作用。
            accepted, user, created = await self.store.run("accept_turn", self.session_id, text, request_id)
            if not created:
                rows = await self.store.run("turns", self.session_id)
                previous = next(row for row in rows if row["id"] == accepted)
                if previous["status"] not in {"completed", "failed", "interrupted", "cancelled"}:
                    raise KakiError("此请求已经被接受且尚未结束；不重复执行")
                data = json.loads(previous["result_json"] or "{}")
                if "turn_id" not in data:
                    raise KakiError("此请求已中断；请查看恢复信息并提交新请求")
                await self.emit("request.duplicate", accepted, {"request_id": request_id})
                return TurnResult(**data)
            turn = accepted
            self.active_turn = turn
            await self.emit(
                "turn.started", turn, {"session_id": self.session_id, "message_id": user.id, "text": text}
            )
            # 3. 原始历史保持完整；当前输入另传，避免在模型窗口中重复加入。
            history = [m for m in await self.store.run("messages", self.session_id) if m.id != user.id]
            if text.strip() == "/compact":
                window = await self.context.assemble(
                    self.session_id,
                    history,
                    user,
                    self.tools.definitions(),
                    force=True,
                    recovery=self.recovery.context(self.recovery_plan),
                )
                from .types import ModelResponse

                answer = f"上下文已整理：路径={window.metadata['path']}，保留历史{len(window.metadata['included_ids'])}条，估算输入{window.estimated_tokens}/{window.budget.input_limit} tokens。原始历史和归档仍保留。"
                message, _ = await self.store.run(
                    "commit_model", self.session_id, turn, ModelResponse(text=answer)
                )
                await self.emit(
                    "model.committed", turn, {"draft_id": "", "message_id": message.id, "text": answer}
                )
                result = TurnResult(turn, "completed", answer, "manual_compaction")
            else:
                await self.emit("understanding.started", turn)
                # 4. 任务理解最多额外推断一次；关键歧义仍由真实用户回答，不据模型标签授权。
                request = await self.understanding.prepare(user, history)
                ambiguity = self.config.understanding.ambiguity
                rounds = 0
                while (
                    self.config.understanding.enabled
                    and request.rewrite.unresolved
                    and ambiguity.policy == "ask"
                ):
                    if rounds >= ambiguity.max_preflight_rounds:
                        raise InteractionRequired(
                            "clarification", "关键目标仍未明确，请提供具体文件、函数或动作后发起新任务"
                        )
                    answer = await self.decisions.request("clarification", request.rewrite.unresolved[0])
                    request.clarifications.append(
                        {"text": answer["text"], "message_id": answer["message_id"]}
                    )
                    rounds += 1
                    self.understanding.refine(request, history)
                await self.store.run(
                    "trace", self.session_id, turn, "understanding.completed", request.metadata()
                )
                await self.emit("understanding.completed", turn, request.metadata())
                # 5. 进入公共主循环；App/TUI 的输入差异到这里已收敛。
                result = await AgentLoop(self).run(
                    turn,
                    self.session_id,
                    user,
                    history,
                    self.recovery.context(self.recovery_plan),
                    request,
                    before_mutation,
                )
        except asyncio.CancelledError:
            if not turn:
                raise
            result = TurnResult(
                turn,
                "cancelled",
                self.partial_text or "已取消当前任务；已完成副作用不会自动撤回。",
                "user_cancel",
            )
        except InteractionRequired as exc:
            if not turn:
                raise
            result = TurnResult(turn, "interrupted", str(exc), exc.category)
        except Exception as exc:
            if not turn:
                raise
            result = TurnResult(
                turn,
                "failed",
                self.partial_text or "任务执行失败：" + safe_error(exc),
                getattr(exc, "category", "execution_error"),
                error=safe_error(exc),
            )
        finally:
            try:
                if result and turn:
                    if self.question and not self.question["future"].done():
                        self.question["future"].cancel()
                    self.question = None
                    # 6. 收尾先完成必要 after 快照；进程退出则保留警告，避免无限延迟关闭。
                    if before is not None and not self.closed:
                        try:
                            snapshot = await asyncio.shield(self.checkpoint.capture(turn, "after"))
                            after = snapshot["id"]
                            if snapshot["state"] != "ready":
                                warnings.append(snapshot.get("error", "结束快照失败"))
                                await self.emit("snapshot.warning", turn, snapshot)
                        except Exception as exc:
                            warnings.append("结束快照失败：" + safe_error(exc))
                    elif before is not None:
                        warnings.append("程序退出，未等待结束快照；会话与已确定执行结果仍保留")
                    result.before_snapshot = before
                    result.after_snapshot = after
                    result.usage = await self.store.run("usage", turn)
                    # Loop exceptions must not erase already committed executions/tests.
                    # 终态统计以已提交日志为准，后续 ReadTimeout 不能抹掉前面真实工具和测试结果。
                    runs = await self.store.run("tool_runs", turn)
                    committed = [row for row in runs if row["result_json"]]
                    result.tools = len(committed)
                    result.tool_failures = 0
                    evidence = []
                    for row in committed:
                        observation = ToolResult(**json.loads(row["result_json"]))
                        result.tool_failures += int(observation.failed)
                        call = ToolCall(row["tool_call_id"], row["name"], json.loads(row["arguments"]))
                        item = verification_evidence(self, call, observation, row["id"])
                        if item:
                            evidence.append(item)
                    result.verification = {
                        "status": verification_status(evidence),
                        "commands": evidence,
                    }
                    try:
                        await self.context.after_turn(self.session_id, user, result)
                    except Exception as exc:
                        warnings.append("派生上下文更新失败，原始执行事实仍保留：" + safe_error(exc))
                    # 最后提交唯一终态，再通知 UI；最终文本先出现并不代表这一阶段已经结束。
                    await self.store.run("finish_turn", result)
                    await self.emit("turn.finished", turn, as_result(result, warnings))
                    self.recovery_plan = {}
            finally:
                self.active_turn = ""
                self.lease.release()
        return result

    async def ask_user(self, question: str, options: list[str]) -> str:
        # 主模型的任务问答也交给持久交互服务；回答不等同于安装工具或任意权限授予。
        response = await self.decisions.request("task", question, options)
        return response["text"]

    async def answer(self, question_id: str, text: str, choice: str | None = None):
        # 所有 UI 共用同一回答校验入口，不能由前端自己解除执行等待。
        await self.decisions.answer(question_id, text, choice)

    async def cancel(self):
        # 仅停止当前 Turn 并等待其 finally；已完成副作用保留，不做隐式回滚。
        if self.active and not self.active.done():
            self.active.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.active

    async def close(self):
        # 退出资源的总入口：先标记关闭，再取消任务，最后关 MCP、模型客户端与 Store。
        if self.closed:
            return
        # 先关闭接单/新快照通道，再取消活动任务并逆序释放资源。
        self.closed = True
        await self.cancel()
        await self.browser.close()
        if self.provider and hasattr(self.provider, "close"):
            await self.provider.close()
        await self.store.close()

    @staticmethod
    def is_test_command(command: str) -> bool:
        # 识别已支持的测试模板，用于证据汇总；不是根据助手口头说“通过”生成结论。
        return bool(
            re.search(
                r"(?:^|[;&|]\s*|\s)(?:pytest|python(?:3)?\s+-m\s+(?:unittest|pytest)|make\s+(?:docker-)?test|go\s+test|npm\s+(?:run\s+)?test|cargo\s+test)(?:\s|$)",
                command,
            )
        )


def as_result(result: TurnResult, warnings: list[str]) -> dict:
    # 把终态及附加警告投影为 UI DTO；数据库终态与 UI 展示使用同一结果对象。
    from dataclasses import asdict

    return {
        **asdict(result),
        "warnings": warnings,
        "recovery_available": bool(result.before_snapshot and result.after_snapshot and not warnings),
    }
