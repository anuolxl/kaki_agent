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
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, Footer, Header, Input, RichLog, Static

from kaki.config import safe_error
from kaki.types import Event, KakiError


class ChatMessage(Static):
    def __init__(self, role: str, text: str, **kwargs):
        super().__init__(**kwargs)
        self.role = role
        self.text = text
        self.add_class("user-message" if role == "user" else "assistant-message")

    def render(self):
        return RichMarkdown(f"**{'你' if self.role == 'user' else 'Kaki'}**\n\n{self.text or '…'}")

    def append(self, text: str):
        self.text += text
        self.refresh(layout=True)

    def replace(self, text: str):
        self.text = text
        self.refresh(layout=True)


class KakiApp(App):
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
    #controls { height: 3; padding: 0 1; }
    #prompt { width: 1fr; }
    Button { min-width: 8; margin-left: 1; }
    Footer { background: #16354a; }
    """
    BINDINGS = [
        ("ctrl+c", "cancel", "取消任务"),
        ("ctrl+n", "new", "新会话"),
        ("ctrl+l", "sessions", "会话"),
        ("ctrl+q", "quit_safe", "退出"),
    ]

    def __init__(self, runtime, resume: str | None = None, **kwargs):
        kwargs.setdefault("ansi_color", False)
        super().__init__(**kwargs)
        self.runtime = runtime
        self.resume = resume
        self.event_queue = asyncio.Queue(maxsize=512)
        self.bubbles = {}
        self.pending = {}
        self.question_id = None
        self.ready_event = asyncio.Event()
        self.turn_done = asyncio.Event()
        self.last_result = None
        self.current_estimate = 0
        self._consumer = None
        self._kaki_quitting = False
        self.undo_plan = None
        self.runtime.listeners.append(self.enqueue)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("", id="workspace")
        yield VerticalScroll(id="chat")
        yield RichLog(id="tools", wrap=True, markup=False, max_lines=300)
        yield Static("初始化Kaki…", id="status")
        with Horizontal(id="controls"):
            yield Input(placeholder="输入任务，Enter发送；/help 查看命令", id="prompt")
            yield Button("发送", id="send", variant="primary")
            yield Button("停止", id="stop", variant="warning")
        yield Footer()

    async def enqueue(self, event: Event):
        await self.event_queue.put(event)

    async def on_mount(self):
        self._consumer = asyncio.create_task(self.consume_events())
        self.set_interval(0.06, self.flush_tokens)
        self.initialize_runtime()

    @work
    async def initialize_runtime(self):
        try:
            await self.runtime.initialize(self.resume)
            await self.load_history()
            self.header_update()
            self.query_one("#status", Static).update("就绪 · Enter发送任务 · Ctrl+C取消 · /help查看命令")
            self.query_one("#prompt", Input).focus()
            self.ready_event.set()
        except Exception as exc:
            self.query_one("#status", Static).update("初始化失败：" + safe_error(exc))
            self.query_one("#tools", RichLog).write(safe_error(exc))
            self.ready_event.set()

    def header_update(self):
        self.query_one("#workspace", Static).update(
            f"工作目录  {self.runtime.workspace.root}\n模型  {self.runtime.config.agents.defaults.model}    会话  {self.runtime.session_id}"
        )

    async def bubble(self, key: str, role: str, text: str):
        if key in self.bubbles:
            self.bubbles[key].replace(text)
            return
        widget = ChatMessage(role, text)
        self.bubbles[key] = widget
        await self.query_one("#chat", VerticalScroll).mount(widget)
        self.query_one("#chat", VerticalScroll).scroll_end(animate=False)

    async def load_history(self):
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
                    await self.bubble(str(d["message_id"]), "user", d["text"])
                    self.query_one("#status", Static).update("任务运行中…")
                elif event.type == "model.started":
                    self.query_one("#status", Static).update(
                        f"模型调用 · 第{d['iteration']}轮 · 当前输入估算{self.current_estimate} tokens"
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
                    self.question_id = d["question_id"]
                    await self.bubble(
                        "question-" + self.question_id,
                        "assistant",
                        d["question"] + "\n" + " / ".join(d.get("options", [])),
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
                        "completed": "已结束",
                        "interrupted": "已中断",
                        "cancelled": "已取消",
                        "failed": "失败",
                    }
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
        pending = self.pending
        self.pending = {}
        for key, text in pending.items():
            if key in self.bubbles:
                self.bubbles[key].append(text)
            else:
                await self.bubble(key, "assistant", text)
        if pending:
            self.query_one("#chat", VerticalScroll).scroll_end(animate=False)

    @on(Input.Submitted, "#prompt")
    async def on_submit(self, event: Input.Submitted):
        text = event.value.strip()
        if not text:
            return
        event.input.value = ""
        await self.submit_text(text)

    @on(Button.Pressed, "#send")
    async def on_send(self):
        field = self.query_one("#prompt", Input)
        text = field.value.strip()
        field.value = ""
        if text:
            await self.submit_text(text)

    @on(Button.Pressed, "#stop")
    async def on_stop(self):
        await self.action_cancel()

    async def submit_text(self, text: str):
        try:
            if self.question_id:
                identity = self.question_id
                self.question_id = None
                await self.runtime.answer(identity, text)
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
            self.turn_done.set()

    @work
    async def watch_turn(self, task):
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self.query_one("#status", Static).update(safe_error(exc))
            self.turn_done.set()

    async def command(self, text: str):
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
                "/new 新会话 · /sessions 会话列表 · /resume ID/last 恢复 · /compact 手动整理上下文 · /context 预算诊断 · /memory 项目记忆 · /undo [turn_id] 文件恢复预览 · /quit 退出。\nCtrl+C仅取消任务，不自动回滚文件。",
            )
        else:
            raise KakiError("未知命令，使用 /help")

    async def action_new(self):
        try:
            await self.runtime.new_session()
            await self.load_history()
            self.header_update()
            self.query_one("#status", Static).update("新会话已就绪")
        except Exception as exc:
            self.query_one("#status", Static).update(safe_error(exc))

    async def action_sessions(self):
        rows = await self.runtime.store.run("sessions")
        await self.bubble(
            "sessions-" + str(len(self.bubbles)),
            "assistant",
            "当前项目会话（/resume ID切换）：\n" + "\n".join(f"- {s['id']} · {s['title']}" for s in rows),
        )

    async def action_cancel(self):
        await self.runtime.cancel()

    async def action_quit_safe(self):
        if self._kaki_quitting:
            return
        self._kaki_quitting = True
        await self.runtime.close()
        self.exit()

    async def on_unmount(self):
        if self._consumer:
            self._consumer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._consumer
        await self.runtime.close()

    async def wait_for_turn(self, timeout=180):
        await asyncio.wait_for(self.turn_done.wait(), timeout)

    async def save_evidence(self, path: Path):
        await self.flush_tokens()
        path.parent.mkdir(parents=True, exist_ok=True)
        svg = self.export_screenshot(title="Kaki Agent · 实际TUI渲染")
        path.write_text(terminal_svg(svg))


def terminal_svg(svg: str) -> str:
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
