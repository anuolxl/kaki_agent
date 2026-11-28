"""App-owned projections and jobs; Runtime remains the owner of durable Turn facts."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from markdown_it import MarkdownIt
from markdown_it.common.utils import escapeHtml

from kaki.config import Config, kaki_home, safe_error
from kaki.runtime import Runtime
from kaki.types import Event, KakiError
from kaki.workspace import Workspace

BUSY_PHASES = {"preparing", "running", "model", "tool", "compacting", "waiting_user", "finishing", "stopping"}
PHASE_LABELS = {
    "idle": "就绪",
    "preparing": "正在准备",
    "running": "正在运行",
    "model": "模型正在思考",
    "tool": "正在执行工具",
    "compacting": "正在整理上下文",
    "waiting_user": "等待你的回答",
    "finishing": "正在收尾，请稍候",
    "stopping": "正在停止",
    "completed": "本轮已完成，可以继续提问",
    "failed": "本轮失败",
    "cancelled": "本轮已停止",
    "interrupted": "本轮未完成，可继续处理",
}
MARKDOWN = MarkdownIt("commonmark", {"html": False, "breaks": True})
MARKDOWN.renderer.rules["image"] = lambda tokens, index, *_: escapeHtml("[图片] " + tokens[index].content)


def render_markdown(text: str) -> str:
    # Markdown HTML is disabled; remote images do not load implicitly into a privileged App window.
    # 把模型正文转换为受限 HTML；不允许模型 HTML 或远端图片进入本机特权窗口。
    return MARKDOWN.render(text)


@dataclass
class Engine:
    # 每个项目一个 UI 执行投影，持有一个 Runtime；它不是数据库中的 Session 或 Turn。
    # 当前项目的内核实例，生命周期归 Controller。
    runtime: Runtime
    # App 自己的接单任务；准备阶段可能还没有 Runtime.active。
    task: asyncio.Task | None = None
    # 执行/最后执行会话，不是窗口正在浏览的 sid。
    session: str = ""
    # 实时 UI 阶段，不直接当作数据库 Turn.status。
    phase: str = "idle"
    # Unix 秒，用于显示经过时间，不是任务正确性证据。
    started: float | None = None
    # 接收 turn.started 后才得到真实任务 ID。
    turn_id: str = ""
    # 按本地模型调用 ID 保存完整草稿投影，客户端应替换而非再追加。
    draft: dict = field(default_factory=dict)
    # 按 tool_call_id 合并进度，与持久历史卡片去重。
    tools: dict = field(default_factory=dict)
    # 当前轮可见警告，不含原始认证异常。
    warnings: list[str] = field(default_factory=list)
    # Task 返回后的结果投影；持久终态仍在 Store。
    result: dict | None = None
    # Turn 建立前暂存的用户输入，接收成功后清空防重复显示。
    pending: str = ""
    # 60ms 合并推送的定时器句柄，退出时取消。
    notification: asyncio.TimerHandle | None = None

    @property
    def busy(self):
        # 以实际 Task 是否结束判断占用；收到完整模型文本并不代表资源已收尾。
        return self.task is not None and not self.task.done()

    def live(self):
        # 生成可序列化实时快照；过滤交互 Future，区分执行会话和用户正在浏览的会话。
        q = self.runtime.question
        return {
            "workspace_id": self.runtime.workspace.id,
            "session_id": self.session,
            "busy": self.busy,
            "phase": self.phase,
            "label": PHASE_LABELS[self.phase],
            "started_at": self.started,
            "turn_id": self.turn_id,
            "drafts": self.draft.copy(),
            "tools": list(self.tools.values()),
            "warnings": self.warnings[:],
            "result": self.result,
            "pending_text": self.pending,
            "question": {
                k: q[k] for k in ("id", "question", "options", "kind", "operation", "session_id", "turn_id")
            }
            if q
            else None,
        }


class AppController:
    # App 层资源所有者：管理项目 Engine、事件订阅与 UI 会话操作，不重写 AgentLoop。
    def __init__(self, config: Config, *, runtime_factory=Runtime):
        self.config, self.runtime_factory = config, runtime_factory
        # 每工作区一个 Engine/Runtime；同项目的多会话共用该执行内核。
        self.engines: dict[str, Engine] = {}
        # 各 WS 客户端独立队列，慢浏览器不能卡住全体执行。
        self.subscribers: set[asyncio.Queue] = set()
        # 仅串行资源创建；不能把长模型/网络请求放在这把锁里。
        self.lock = asyncio.Lock()
        self.closed = False
        # 实时通知序号，与 SQLite events.seq 的持久日志序号分开。
        self.seq = 0
        # 只保存项目导航路径，不保存对话正文或模型凭据。
        self.registry = kaki_home() / "app-workspaces.json"
        self.paths = self._read_paths()

    def _read_paths(self):
        # 载入项目导航注册表；损坏时从工作区元数据恢复，不把它当会话正文数据库。
        if self.registry.exists():
            try:
                data = json.loads(self.registry.read_text())
                if data.get("schema_version") == 1 and isinstance(data.get("paths"), list):
                    return list(dict.fromkeys(p for p in data["paths"] if isinstance(p, str)))[:50]
            except (ValueError, OSError):
                pass
        paths = []
        for file in sorted(
            (kaki_home() / "projects").glob("*/workspace.json"), key=lambda p: p.stat().st_mtime, reverse=True
        ):
            try:
                value = json.loads(file.read_text())["root"]
                if Path(value).is_dir() and not value.startswith(
                    ("/private/tmp/", "/tmp/", "/private/var/folders/")
                ):
                    paths.append(value)
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return list(dict.fromkeys(paths))[:50]

    def _save_paths(self):
        # 原子保存最近项目路径；导航配置失败不应产生半个 JSON 文件。
        self.registry.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, path = tempfile.mkstemp(prefix=".app-workspaces-", dir=self.registry.parent)
        try:
            with os.fdopen(fd, "w") as out:
                json.dump({"schema_version": 1, "paths": self.paths}, out, ensure_ascii=False)
                out.flush()
                os.fsync(out.fileno())
            os.replace(path, self.registry)
        finally:
            if Path(path).exists():
                Path(path).unlink()

    def publish(self, kind: str, **data):
        # 给浏览器广播投影；慢客户端通过 resync 重读事实，不能用丢失的增量推断完成。
        self.seq += 1
        value = {"type": kind, "seq": self.seq, **data}
        for queue in tuple(self.subscribers):
            if queue.full():
                # A slow/reconnected viewer must reload authoritative state, never infer completion from a dropped delta.
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait({"type": "resync", "seq": self.seq})
            else:
                queue.put_nowait(value)

    def notify(self, engine: Engine, *, reload=False):
        # 按定时器合并高频更新，控制 UI 刷新成本；不延迟 Store 对执行结果的提交。
        if engine.notification:
            engine.notification.cancel()
            engine.notification = None
        self.publish(
            "workspace.changed", workspace_id=engine.runtime.workspace.id, live=engine.live(), reload=reload
        )

    async def observe(self, engine: Engine, event: Event):
        # Runtime 事件到 App phase 的适配表；模型提交与 Turn 终态仍要经过 _execute 收尾。
        data = event.data
        match event.type:
            case "turn.started":
                engine.turn_id = event.turn_id
                engine.phase = "running"
                engine.pending = ""
            case "model.started":
                engine.phase = "model"
            case "understanding.started":
                engine.phase = "preparing"
            case "understanding.completed" | "question.answered":
                engine.phase = "running"
            case "model.delta":
                key = data["draft_id"]
                engine.draft[key] = engine.draft.get(key, "") + data["text"]
                if engine.notification is None:
                    engine.notification = asyncio.get_running_loop().call_later(
                        0.06, lambda: self.notify(engine)
                    )
                return
            case "model.committed":
                engine.draft.pop(data.get("draft_id", ""), None)
                if data.get("text") and not data.get("tool_count"):
                    engine.phase = "finishing"
            case "tool.started":
                engine.phase = "waiting_user" if data["name"] == "kaki_ask_user" else "tool"
                engine.tools[data["tool_call_id"]] = {**data, "state": "running"}
            case "tool.finished":
                engine.tools[data["tool_call_id"]] = {
                    **engine.tools.get(data["tool_call_id"], {}),
                    **data,
                    "state": data.get("state", "failed" if data["failed"] else "succeeded"),
                }
            case "question.requested":
                engine.phase = "waiting_user"
            case "context.compacted" | "turn.finalizing":
                engine.phase = "compacting"
            case "runtime.status":
                if "快照" in data["text"]:
                    engine.phase = "preparing" if "基线" in data["text"] else "finishing"
            case "snapshot.warning":
                engine.warnings.append(data.get("error", "文件快照不可用"))
            case "runtime.warning":
                engine.warnings.append(data["text"])
            case "turn.finished":
                # The Runtime Task has not returned yet: keep controls busy until _execute awaits it.
                engine.result = data
                engine.phase = "finishing"
            case _:
                pass
        self.notify(
            engine,
            reload=event.type
            in {"turn.started", "model.committed", "tool.started", "tool.finished", "turn.finished"},
        )

    async def open_workspace(self, path: str):
        # 串行创建项目 Engine，避免同一工作区重复 Runtime；打开历史时不急于连接浏览器 MCP。
        if self.closed:
            raise KakiError("App正在退出")
        if not path.strip() or len(path) > 4096:
            raise KakiError("请输入有效的仓库绝对路径")
        async with self.lock:
            ws = await asyncio.to_thread(Workspace.resolve, Path(path), self.config)
            if ws.id in self.engines:
                return self.engines[ws.id]
            rt = self.runtime_factory(ws, self.config)
            engine = Engine(rt)
            rt.listeners.append(lambda event: self.observe(engine, event))
            try:
                await rt.store.open()
                rows = await rt.store.run("sessions")
                selected = None
                for row in rows:
                    models = await rt.store.run("session_models", row["id"])
                    if all(
                        m.removeprefix("openai/") == self.config.agents.defaults.model.removeprefix("openai/")
                        for m in models
                    ):
                        selected = row["id"]
                        break
                await rt.initialize(selected, connect_browser=False)
                engine.session = rt.session_id
                # An existing process holding the lease retains its running records.
                try:
                    rt.lease.acquire()
                except KakiError:
                    pass
                else:
                    try:
                        for row in rows:
                            await rt.store.run("recover", row["id"])
                    finally:
                        rt.lease.release()
            except BaseException:
                await rt.close()
                raise
            self.engines[ws.id] = engine
            self.paths = [str(ws.root), *(p for p in self.paths if p != str(ws.root))][:50]
            await asyncio.to_thread(self._save_paths)
            self.publish("projects.changed")
            return engine

    def engine(self, identity: str):
        # 只访问已打开的项目，避免一个拼错的 workspace_id 隐式创建资源。
        if identity not in self.engines:
            raise KakiError("请先打开该工作区")
        return self.engines[identity]

    def projects(self):
        # 项目导航来自注册路径；看到项目条目不等于该项目已加载 Runtime。
        return [
            {
                "path": path,
                "name": Path(path).name,
                "workspace_id": next(
                    (k for k, e in self.engines.items() if str(e.runtime.workspace.root) == path), None
                ),
            }
            for path in self.paths
        ]

    async def sessions(self, workspace: str, archived=False):
        # 读取会话摘要供侧边栏显示，支持归档视图；不改变当前执行会话。
        e = self.engine(workspace)
        return await e.runtime.store.run("session_summaries", archived)

    async def detail(self, workspace: str, session: str):
        # 组合持久历史与内存 live 投影；这些字段的来源和恢复保证不同。
        e = self.engine(workspace)
        store = e.runtime.store
        metadata = await store.run("session", session)
        messages = await store.run("messages", session)
        turns = await store.run("turns", session)
        visible = [
            {
                "id": m.id,
                "turn_id": m.turn_id,
                "role": m.role,
                "text": m.content,
                "html": render_markdown(m.content),
                "created_at": m.created_at,
            }
            for m in messages
            if m.role in ("user", "assistant") and m.content
        ]
        turn_values = []
        for row in turns:
            result = json.loads(row["result_json"] or "{}")
            turn_values.append(
                {k: row[k] for k in ("id", "status", "text", "stop_reason", "error", "created", "ended")}
                | {"result": result}
            )
            runs = await store.run("tool_runs", row["id"])
            turn_values[-1]["tools"] = [
                {
                    "tool_call_id": run["tool_call_id"],
                    "name": run["name"],
                    "state": run["state"],
                    "arguments": json.loads(run["arguments"]),
                    "preview": json.loads(run["result_json"] or "{}").get("text", "")[:1600],
                }
                for run in runs
            ]
        return {
            "session": metadata,
            "messages": visible,
            "turns": turn_values,
            "live": e.live(),
            "model": self.config.agents.defaults.model,
            "context": await store.run("context_get", session),
            "seq": self.seq,
        }

    async def new_session(self, workspace, title="新会话"):
        # 创建会话供界面浏览；不切换正在执行中的 Runtime。
        e = self.engine(workspace)
        identity = await e.runtime.store.run("new_session", title[:80] or "新会话")
        self.notify(e, reload=True)
        return identity

    async def rename(self, workspace, session, title):
        # 只修改导航标题，不能改变会话中的原始消息或执行状态。
        await self.engine(workspace).runtime.store.run("rename_session", session, title)
        self.publish("projects.changed")

    async def archive(self, workspace, session, archived):
        # 归档是隐藏会话而非删除事实；准备期尚无 Turn 时也需要 busy 检查。
        e = self.engine(workspace)
        if e.busy and e.session == session:
            raise KakiError("请先停止或等待本轮任务结束")
        await e.runtime.store.run("archive_session", session, archived)
        self.publish("projects.changed")

    async def send(self, workspace: str, session: str, text: str, request_id=None):
        # 异步检查后再次确认 busy，再创建 Controller Task，防止并发 HTTP 请求双重接单。
        e = self.engine(workspace)
        if self.closed:
            raise KakiError("App正在退出")
        if e.busy:
            raise KakiError("该仓库已有任务正在运行，请等待或停止后再发送")
        if not text.strip() or len(text) > 100000:
            raise KakiError("消息需要1–100000个字符")
        row = await e.runtime.store.run("session", session)
        if row["archived"]:
            raise KakiError("请先恢复已归档会话")
        # Recheck after the asynchronous lookup, before installing the owned Task.
        if e.busy:
            raise KakiError("该仓库已有任务正在运行")
        e.session = session
        e.phase = "preparing"
        e.started = time.time()
        e.turn_id = ""
        e.draft = {}
        e.tools = {}
        e.warnings = []
        e.result = None
        e.pending = text
        e.task = asyncio.create_task(self._execute(e, text, request_id), name="kaki-app-turn")
        self.notify(e)

    async def _execute(self, e: Engine, text, request_id):
        # App 接单后的真实任务所有者；切会话、准备 MCP、等待 Runtime.run 后才解除 busy。
        try:
            if e.runtime.session_id != e.session:
                await e.runtime.switch_session(e.session)
            await e.runtime.ensure_browser()
            result = await e.runtime.run(text, request_id)
            e.result = e.result or asdict(result)
            e.phase = result.status
        except asyncio.CancelledError:
            await e.runtime.cancel()
            e.phase = "cancelled"
            if e.result is None:
                e.result = {"status": "cancelled", "text": "已停止本轮任务", "stop_reason": "user_cancel"}
        except Exception as exc:
            e.phase = "failed"
            e.result = {
                "status": "failed",
                "text": safe_error(exc),
                "error": safe_error(exc),
                "stop_reason": "app_error",
            }
        finally:
            e.draft = {}
            e.pending = ""
            e.task = None
            self.notify(e, reload=True)

    async def stop(self, workspace: str, session: str):
        # 停止本轮：准备阶段取消 Controller Task，内核已接单则交给 Runtime.cancel。
        e = self.engine(workspace)
        if e.session != session or not e.busy:
            raise KakiError("该会话没有可停止的本轮任务")
        e.phase = "stopping"
        self.notify(e)
        # When Runtime already owns a Turn, let it handle cancellation and finish its journal itself.
        if e.runtime.active and not e.runtime.active.done():
            await e.runtime.cancel()
        elif e.task:
            e.task.cancel()
        task = e.task
        if task:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def answer(self, workspace, session, question_id, text, choice=None):
        # 回答必须属于当前活跃会话与问题；前端浏览其他会话不能误答执行中的授权。
        e = self.engine(workspace)
        if e.session != session or not e.busy:
            raise KakiError("该会话没有待回答的问题")
        if not text.strip() or len(text) > 100000:
            raise KakiError("回答不能为空或过长")
        await e.runtime.answer(question_id, text, choice)
        e.phase = "running"
        self.notify(e)

    async def close(self):
        # 整个 App 退出入口；关闭所有项目资源，不把“关闭一个浏览器标签”当退出。
        if self.closed:
            return
        self.closed = True
        for e in self.engines.values():
            if e.notification:
                e.notification.cancel()
        await asyncio.gather(*(self._close_engine(e) for e in self.engines.values()))
        self.publish("app.closed")

    async def _close_engine(self, e):
        # 逐项目取消刷新定时器与任务并释放 Runtime，防止后台残留。
        task = e.task
        if task and not task.done() and not (e.runtime.active and not e.runtime.active.done()):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        # Runtime.close marks shutdown before cancelling: avoid a new slow after-snapshot on process exit.
        await e.runtime.close()
        if task:
            with contextlib.suppress(asyncio.CancelledError):
                await task
