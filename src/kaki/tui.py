"""Actual Textual UI: streaming drafts are upserted, not appended a second time."""

from __future__ import annotations

import asyncio
import contextlib
import json
import xml.etree.ElementTree as ET
from pathlib import Path

from rich.cells import cell_len
from rich.markdown import Markdown as RichMarkdown
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, Footer, Header, Input, RichLog, Static

from kaki.config import safe_error
from kaki.types import Event, KakiError


class ChatMessage(Static):
    # 一条界面气泡；草稿和最终文本通过 key 对齐，不额外代表一个会话或模型调用。
    def __init__(self, role: str, text: str, **kwargs):
        super().__init__(**kwargs)
        self.role = role
        self.text = text
        self.add_class("user-message" if role == "user" else "assistant-message")

    def render(self):
        # 正文以 Markdown 展示；UI 文本缓冲与 Store 原始消息是不同层次。
        return RichMarkdown(f"**{'你' if self.role == 'user' else 'Kaki'}**\n\n{self.text or '…'}")

    def append(self, text: str):
        # 合并尚在流式生成的正文；最终提交时使用 replace 避免重复显示。
        self.text += text
        self.refresh(layout=True)

    def replace(self, text: str):
        # 完整提交或重载以当前文本替换，避免 delta 加最终正文重复。
        self.text = text
        self.refresh(layout=True)


class KakiApp(App):
    # Textual 交互适配器：接收输入、消费事件和显示状态，执行与持久化交给 Runtime。
    TITLE = "Kaki Agent"
    CSS = """
    Screen { background: #0d1524; color: #e3edf9; }
    Header { background: #16354a; }
    #workspace { height: 3; padding: 0 2; background: #142435; color: #91cde6; }
    #chat { height: 1fr; padding: 1 2; }
    ChatMessage { width: 1fr; height: auto; min-height: 3; margin: 0 0 1 0; padding: 1 2; }
    .user-message { background: #173346; border-left: thick #38bdf8; }
    .assistant-message { background: #182638; border-left: thick #34d399; }
    #tools { height: 7; border-top: solid #29485e; background: #101e2e; padding: 0 2; }
    #status { height: 2; padding: 0 2; color: #7dd3fc; background: #142435; }
    #task-phase { height: 3; content-align: center middle; text-style: bold; background: #142435; color: #9fb7cc; }
    #task-phase.running { background: #4a3216; color: #ffd189; }
    #task-phase.completed { background: #12412e; color: #9df0c1; }
    #task-phase.failed { background: #521f29; color: #ffc2cc; }
    #task-phase.cancelled, #task-phase.interrupted { background: #42341d; color: #edd09d; }
    #controls { height: 3; padding: 0 1; }
    #prompt { width: 1fr; }
    Button { min-width: 8; margin-left: 1; }
    Footer { background: #16354a; }
    """
    # Ctrl+C 提升优先级，防止 Textual 默认按键语义截获它，确保退出整个程序。
    BINDINGS = [
        Binding("ctrl+c", "interrupt", "退出程序", priority=True),
        ("ctrl+n", "new", "新会话"),
        ("ctrl+l", "sessions", "会话"),
        ("ctrl+q", "quit_safe", "退出"),
    ]

    def __init__(self, runtime, resume: str | None = None, **kwargs):
        # 建立 UI 队列和测试/生命周期握手对象；这些 Event/Task 都属于当前 asyncio loop。
        kwargs.setdefault("ansi_color", False)
        super().__init__(**kwargs)
        self.runtime = runtime
        self.resume = resume
        # UI 消费队列与执行内核分离，避免模型增量直接驱动同步重排。
        self.event_queue = asyncio.Queue(maxsize=512)
        # 用消息/草稿 ID 对齐已有气泡，完整提交时不重复追加。
        self.bubbles = {}
        # 尚未按60ms节奏刷新的 token 缓冲。
        self.pending = {}
        self.question_id = None
        # 初始化成功或失败都会置位，等待方随后检查真实状态。
        self.ready_event = asyncio.Event()
        # watch_turn 确认整个 Runtime Task 收尾后才表示可进入下一轮。
        self.turn_done = asyncio.Event()
        self.last_result = None
        self.current_estimate = 0
        self._consumer = None
        self._kaki_quitting = False
        # 恢复是先预览后批准，不能凭旧快照覆盖任务后的用户编辑。
        self.undo_plan = None
        self.runtime.listeners.append(self.enqueue)

    def compose(self) -> ComposeResult:
        # 定义控件树；构建页面本身不会创建 Turn 或调用模型。
        yield Header(show_clock=True)
        yield Static("", id="workspace")
        yield Static("正在初始化 Kaki…", id="task-phase")
        yield VerticalScroll(id="chat")
        yield RichLog(id="tools", wrap=True, markup=False, max_lines=300)
        yield Static("初始化Kaki…", id="status")
        with Horizontal(id="controls"):
            yield Input(placeholder="输入任务，Enter发送；/help 查看命令", id="prompt")
            yield Button("发送", id="send", variant="primary", disabled=True)
            yield Button("停止本轮", id="stop", variant="warning", disabled=True)
        yield Footer()

    async def enqueue(self, event: Event):
        # Runtime 监听器只入队，让独立消费者更新控件，避免执行链直接操作 UI。
        await self.event_queue.put(event)

    async def on_mount(self):
        # 先启动持续事件消费与 60ms 刷新，再启动 Textual 初始化 worker。
        self._consumer = asyncio.create_task(self.consume_events())
        self.set_interval(0.06, self.flush_tokens)
        self.initialize_runtime()

    @work
    async def initialize_runtime(self):
        # @work 管理异步初始化而非新线程；成功或失败均释放 ready_event，便于明确诊断。
        try:
            await self.runtime.initialize(self.resume)
            await self.load_history()
            self.header_update()
            self.query_one("#status", Static).update("就绪 · Enter发送 · 停止按钮仅停止本轮 · Ctrl+C退出程序")
            self.query_one("#prompt", Input).focus()
            self.phase("idle", "○ 已就绪 · 可发送任务")
            self.ready_event.set()
        except Exception as exc:
            self.query_one("#status", Static).update("初始化失败：" + safe_error(exc))
            self.query_one("#tools", RichLog).write(safe_error(exc))
            self.ready_event.set()

    def header_update(self):
        # 只投影当前工作区、模型和会话；不把完整配置（含 Key）放入 Header。
        self.query_one("#workspace", Static).update(
            f"工作目录  {self.runtime.workspace.root}\n模型  {self.runtime.config.agents.defaults.model}    会话  {self.runtime.session_id}"
        )

    async def bubble(self, key: str, role: str, text: str):
        # 同一消息/草稿 key 采用更新而非再次挂载，关闭流式与最终提交的重复气泡。
        if key in self.bubbles:
            self.bubbles[key].replace(text)
            return
        widget = ChatMessage(role, text)
        self.bubbles[key] = widget
        await self.query_one("#chat", VerticalScroll).mount(widget)
        self.query_one("#chat", VerticalScroll).scroll_end(animate=False)

    async def load_history(self):
        # 从 Store 重绘已有事实；恢复提示只是核验线索，不会执行旧工具。
        self.bubbles = {}
        self.pending = {}
        await self.query_one("#chat", VerticalScroll).remove_children()
        if not self.runtime.session_id:
            return
        for message in await self.runtime.store.run("messages", self.runtime.session_id):
            if message.role in ("user", "assistant") and message.content:
                await self.bubble(str(message.id), message.role, message.content)
        if self.runtime.recovery_plan.get("items"):
            self.query_one("#tools", RichLog).write(
                "已恢复会话；上次中断/未知操作将作为下一任务的核验线索，不自动重放。"
            )

    async def consume_events(self):
        # 持续把领域事件转换成状态、草稿和工具卡片；结束信号还需 watch_turn 等待收尾。
        try:
            while True:
                event = await self.event_queue.get()
                d = event.data
                if event.type == "runtime.status":
                    self.query_one("#status", Static).update(d["text"])
                elif event.type == "runtime.ready":
                    if d.get("browser_warning"):
                        self.query_one("#tools", RichLog).write("浏览器未连接：" + d["browser_warning"])
                    else:
                        self.query_one("#tools", RichLog).write(
                            f"Playwright MCP：{d.get('browser_tools', 0)}个浏览器工具"
                        )
                elif event.type == "turn.started":
                    self.phase("running", "◉ 本轮任务正在运行 · 请等待，或点击停止本轮")
                    await self.bubble(str(d["message_id"]), "user", d["text"])
                    self.query_one("#status", Static).update("任务运行中…")
                elif event.type == "model.started":
                    self.query_one("#status", Static).update(
                        f"模型调用 · 第{d['iteration']}轮 · 当前输入估算{self.current_estimate} tokens"
                    )
                elif event.type == "understanding.started":
                    self.query_one("#status", Static).update("正在识别任务与整理检索线索…")
                elif event.type == "understanding.completed":
                    self.query_one("#tools", RichLog).write(
                        "任务理解：" + json.dumps(d, ensure_ascii=False)[:800]
                    )
                elif event.type == "model.delta":
                    key = "draft-" + d["draft_id"]
                    self.pending[key] = self.pending.get(key, "") + d["text"]
                elif event.type == "model.committed":
                    await self.flush_tokens()
                    draft = "draft-" + d.get("draft_id", "")
                    key = str(d["message_id"])
                    if draft in self.bubbles:
                        widget = self.bubbles.pop(draft)
                        self.bubbles[key] = widget
                        widget.replace(d["text"])
                    elif d["text"]:
                        await self.bubble(key, "assistant", d["text"])
                elif event.type == "tool.started":
                    args = json.dumps(d["arguments"], ensure_ascii=False)
                    self.query_one("#tools", RichLog).write("▶ " + d["name"] + " " + args[:220])
                elif event.type == "tool.finished":
                    self.query_one("#tools", RichLog).write(
                        ("✗ " if d["failed"] else "✓ ")
                        + d["name"]
                        + " · "
                        + d["preview"].replace("\n", " ")[:250]
                    )
                elif event.type in ("context.assembled", "context.compacted"):
                    self.current_estimate = d.get("estimated_tokens", self.current_estimate)
                    self.query_one("#tools", RichLog).write(
                        f"上下文：{d.get('path', 'rebalance')} · 估算{self.current_estimate} tokens · Skills={d.get('injected_skill_ids', [])}"
                    )
                elif event.type == "snapshot.warning":
                    self.query_one("#tools", RichLog).write("快照警告：" + d.get("error", "不可用"))
                elif event.type == "question.requested":
                    self.phase("running", "? 等待你的回答 · 本轮任务尚未结束", waiting=True)
                    self.question_id = d["question_id"]
                    await self.bubble(
                        "question-" + self.question_id,
                        "assistant",
                        {
                            "approval": "【操作授权】",
                            "clarification": "【任务澄清】",
                            "task": "【补充信息】",
                        }.get(d.get("kind"), "")
                        + d["question"]
                        + "\n"
                        + " / ".join(d.get("options", [])),
                    )
                    self.query_one("#status", Static).update("等待你的回答；输入框中直接回复，或Ctrl+C取消")
                elif event.type == "turn.finalizing":
                    self.query_one("#status", Static).update("迭代预算耗尽，正在总结已有结果…")
                elif event.type == "turn.finished":
                    await self.flush_tokens()
                    self.last_result = d
                    u = d.get("usage", {})
                    v = d.get("verification", {})
                    labels = {
                        "completed": "✓ 本轮已完成，可以继续提问",
                        "interrupted": "! 本轮未完成，可继续处理",
                        "cancelled": "■ 本轮已停止，可以继续提问",
                        "failed": "! 本轮失败，请查看错误",
                    }
                    self.phase(d["status"], labels[d["status"]])
                    self.query_one("#status", Static).update(
                        f"{labels[d['status']]} · {d['stop_reason']} · 本轮累计{u.get('total_tokens', 0)} tokens / {u.get('calls', 0)}次模型请求 · 验证 {v.get('status', 'not_run')}"
                    )
                    if d.get("error"):
                        self.query_one("#tools", RichLog).write("错误：" + d["error"])
                    if d["status"] == "cancelled" and not self.bubbles:
                        await self.bubble("cancel-" + event.turn_id, "assistant", d["text"])
                    self.turn_done.set()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.query_one("#status", Static).update("界面事件错误：" + safe_error(exc))
            self.turn_done.set()

    async def flush_tokens(self):
        # 定时批量刷新流式片段，减少每个 token 都重排 Markdown 带来的开销。
        pending = self.pending
        self.pending = {}
        for key, text in pending.items():
            if key in self.bubbles:
                self.bubbles[key].append(text)
            else:
                await self.bubble(key, "assistant", text)
        if pending:
            self.query_one("#chat", VerticalScroll).scroll_end(animate=False)

    def phase(self, state: str, text: str, *, waiting=False):
        # 统一忙碌/终态的控件状态，避免只有“有文本”却看不出是否仍在执行。
        banner = self.query_one("#task-phase", Static)
        banner.set_classes(state)
        banner.update(text)
        self.query_one("#send", Button).disabled = state == "running" and not waiting
        self.query_one("#stop", Button).disabled = state != "running"

    @on(Input.Submitted, "#prompt")
    async def on_submit(self, event: Input.Submitted):
        # Enter 事件只转交 submit_text；输入是任务还是待确认回答由同一入口判断。
        text = event.value.strip()
        if not text:
            return
        event.input.value = ""
        await self.submit_text(text)

    @on(Button.Pressed, "#send")
    async def on_send(self):
        # 发送按钮与 Enter 使用同一入口，避免两套接单与审批逻辑。
        field = self.query_one("#prompt", Input)
        text = field.value.strip()
        field.value = ""
        if text:
            await self.submit_text(text)

    @on(Button.Pressed, "#stop")
    async def on_stop(self):
        # 按钮语义是停止本轮，不能顺带退出整个进程。
        await self.action_cancel()

    async def submit_text(self, text: str):
        # 先处理待回答问题和本地命令，再提交新任务；有效回答后才清空 question_id。
        try:
            if self.question_id:
                identity = self.question_id
                await self.runtime.answer(identity, text)
                self.question_id = None
                return
            if self.undo_plan is not None:
                plan = self.undo_plan
                self.undo_plan = None
                if text.strip().upper() == "RESTORE":
                    self.runtime.lease.acquire()
                    try:
                        r = await self.runtime.checkpoint.apply(
                            plan, [x["path"] for x in plan["paths"] if not x["conflict"]]
                        )
                    finally:
                        self.runtime.lease.release()
                    await self.bubble(
                        "restore-" + str(len(self.bubbles)), "assistant", json.dumps(r, ensure_ascii=False)
                    )
                else:
                    self.query_one("#tools", RichLog).write("已放弃恢复")
                return
            if text.startswith("/") and text != "/compact":
                await self.command(text)
                return
            self.turn_done.clear()
            self.last_result = None
            task = self.runtime.submit(text)
            self.watch_turn(task)
        except Exception as exc:
            self.query_one("#status", Static).update(safe_error(exc))
            self.query_one("#tools", RichLog).write(safe_error(exc))
            if not self.runtime.active or self.runtime.active.done():
                self.turn_done.set()

    @work
    async def watch_turn(self, task):
        # 等待 Runtime.submit 返回的 Task；真正结束后更新最后结果、按钮与 turn_done。
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self.query_one("#status", Static).update(safe_error(exc))
            self.turn_done.set()

    async def command(self, text: str):
        # 斜杠命令属于 UI/会话管理，不作为用户代码任务再交给模型解释。
        command, _, arg = text.partition(" ")
        if command == "/new":
            await self.action_new()
        elif command == "/sessions":
            await self.action_sessions()
        elif command == "/resume":
            await self.runtime.switch_session(arg.strip() or "last")
            await self.load_history()
            self.header_update()
        elif command == "/context":
            await self.bubble(
                "context-" + str(len(self.bubbles)),
                "assistant",
                "```json\n"
                + json.dumps(self.runtime.context.last_metadata, ensure_ascii=False, indent=2)
                + "\n```",
            )
        elif command == "/memory":
            rows = await self.runtime.store.run("memories", self.runtime.config.memory.user_id)
            await self.bubble(
                "memory-" + str(len(self.bubbles)),
                "assistant",
                "\n".join(f"- {r['id']}: {r['content']}" for r in rows) or "当前项目尚无长期记忆。",
            )
        elif command == "/undo":
            if self.runtime.active and not self.runtime.active.done():
                raise KakiError("恢复前请先结束当前任务")
            turns = await self.runtime.store.run("turns", self.runtime.session_id)
            turn = arg.strip() or (turns[-1]["id"] if turns else "")
            plan = await self.runtime.checkpoint.plan_restore(turn)
            self.undo_plan = plan
            await self.bubble(
                "undo-preview-" + str(len(self.bubbles)),
                "assistant",
                "恢复预览：\n"
                + "\n".join(
                    ("- 冲突，不覆盖：" if p["conflict"] else "- 可恢复：") + p["path"] for p in plan["paths"]
                )
                + "\n输入 RESTORE 恢复无冲突文件；其他内容放弃。",
            )
        elif command == "/quit":
            await self.action_quit_safe()
        elif command == "/help":
            await self.bubble(
                "help-" + str(len(self.bubbles)),
                "assistant",
                "/new 新会话 · /sessions 会话列表 · /resume ID/last 恢复 · /compact 手动整理上下文 · /context 预算诊断 · /memory 项目记忆 · /undo [turn_id] 文件恢复预览 · /quit 退出。\n停止按钮仅停止本轮；Ctrl+C退出整个程序。已发生的文件修改不会自动撤回。",
            )
        else:
            raise KakiError("未知命令，使用 /help")

    async def action_new(self):
        # 创建新会话前遵守当前任务的占用限制，避免历史串到另一会话。
        try:
            await self.runtime.new_session()
            await self.load_history()
            self.header_update()
            self.query_one("#status", Static).update("新会话已就绪")
        except Exception as exc:
            self.query_one("#status", Static).update(safe_error(exc))

    async def action_sessions(self):
        # 会话导航需要核对占用状态，不能把浏览切换变成并发执行。
        rows = await self.runtime.store.run("sessions")
        await self.bubble(
            "sessions-" + str(len(self.bubbles)),
            "assistant",
            "当前项目会话（/resume ID切换）：\n" + "\n".join(f"- {s['id']} · {s['title']}" for s in rows),
        )

    async def action_cancel(self):
        # 停止按钮只取消当前 Turn，TUI 仍可继续输入下一轮。
        await self.runtime.cancel()

    async def action_quit_safe(self):
        # 正常退出也统一经过 Runtime 关闭，不使用立即终止绕过日志收尾。
        await self._quit(0)

    async def action_interrupt(self):
        # Ctrl+C 是整个程序退出语义；与“停止本轮”分开，退出码为 130。
        await self._quit(130)

    async def _quit(self, exit_code):
        # 统一退出握手，防止重复信号同时关闭相同资源。
        if self._kaki_quitting:
            return
        self._kaki_quitting = True
        await self.runtime.close()
        self.exit(return_code=exit_code)

    async def on_unmount(self):
        # 卸载时取消事件消费者并关闭 Runtime，避免孤立协程或 MCP 子进程。
        if self._consumer:
            self._consumer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._consumer
        await self.runtime.close()

    async def wait_for_turn(self, timeout=180):
        # 给真实 TUI 验证等待完整任务的事件边界，不靠固定睡眠猜测执行结束。
        await asyncio.wait_for(self.turn_done.wait(), timeout)

    async def save_evidence(self, path: Path):
        # 导出实际控件渲染用于验证；不是生成一张模拟界面截图。
        await self.flush_tokens()
        path.parent.mkdir(parents=True, exist_ok=True)
        svg = self.export_screenshot(title="Kaki Agent · 实际TUI渲染")
        path.write_text(terminal_svg(svg))


def terminal_svg(svg: str) -> str:
    # 把 Textual 原生导出的 SVG 包装为证据文件，不改变对话或任务状态。
    """Rich export uses codepoint length for textLength; CJK occupies two terminal cells.

    Correct only font metrics for exported evidence. No UI text, state or results are changed.
    """
    ET.register_namespace("", "http://www.w3.org/2000/svg")
    root = ET.fromstring(svg)
    for text in root.iter("{http://www.w3.org/2000/svg}text"):
        value = "".join(text.itertext())
        if value and "textLength" in text.attrib:
            width = float(text.attrib["textLength"]) * cell_len(value) / len(value)
            text.set("textLength", str(width))
            text.set("lengthAdjust", "spacingAndGlyphs")
    return ET.tostring(root, encoding="unicode")
