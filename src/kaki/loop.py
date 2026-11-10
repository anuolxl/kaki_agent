"""The bounded model -> tool -> observation loop; Runtime alone publishes terminal state."""

from __future__ import annotations

import asyncio
import time

from kaki.types import Message, ModelResponse, ProviderError, TurnResult


class AgentLoop:
    def __init__(self, runtime):
        self.runtime = runtime

    async def run(
        self, turn: str, session: str, user: Message, history: list[Message], recovery: str = ""
    ) -> TurnResult:
        rt = self.runtime
        cfg = rt.config.agents.defaults
        definitions = rt.tools.definitions()
        window = await rt.context.assemble(session, history, user, definitions, recovery=recovery)
        await rt.emit(
            "context.assembled", turn, window.metadata | {"estimated_tokens": window.estimated_tokens}
        )
        failures = 0
        tool_count = 0
        empty_retries = 0
        prefills = 0
        nudges = 0
        previous_tools = False
        compressions = 0
        hard_name = None
        hard_count = 0
        hard_nudges = 0
        verification = {"status": "not_run", "commands": []}
        ctx = {
            "store": rt.store,
            "session_id": session,
            "turn_id": turn,
            "user_message_id": user.id,
            "user_text": user.content,
        }
        draft_last = 0.0
        draft_text = ""

        async def on_delta(text, call_id):
            nonlocal draft_last, draft_text
            draft_text += text
            rt.partial_text = draft_text
            await rt.emit("model.delta", turn, {"draft_id": call_id, "text": text})
            if time.monotonic() - draft_last > 0.7:
                draft_last = time.monotonic()
                await rt.store.run("draft", turn, draft_text)

        iteration = 0
        while iteration < cfg.max_tool_iterations:
            iteration += 1
            if asyncio.current_task().cancelling():
                raise asyncio.CancelledError
            definitions = rt.tools.definitions()
            updated = await rt.context.rebalance(session, window, definitions)
            if updated is not window:
                window = updated
                await rt.emit(
                    "context.compacted", turn, window.metadata | {"estimated_tokens": window.estimated_tokens}
                )
            await rt.emit("model.started", turn, {"iteration": iteration, "model": cfg.model})
            draft_text = ""
            try:
                response = await rt.provider.complete(
                    window.messages, definitions, turn_id=turn, kind="main", on_delta=on_delta
                )
            except ProviderError as exc:
                if exc.category == "context_overflow" and not exc.partial and compressions < 2:
                    from .context.history import shrink_tool_text

                    reduced, elided = shrink_tool_text(window.messages, 3)
                    if elided:
                        window.messages = reduced
                        compressions += 1
                        iteration -= 1
                        await rt.emit(
                            "context.compacted",
                            turn,
                            {"reason": "provider_context_overflow", "elided_tool_results": elided},
                        )
                        continue
                raise
            if response.tool_calls:
                message, runs = await rt.store.run("commit_model", session, turn, response)
                window.messages.append(message)
                await rt.emit(
                    "model.committed",
                    turn,
                    {
                        "draft_id": response.call_id,
                        "message_id": message.id,
                        "text": response.text,
                        "tool_count": len(runs),
                    },
                )
                by_call = {r["tool_call_id"]: r for r in runs}
                observations = {}

                async def started(call):
                    await rt.store.run("mark_running", by_call[call.id]["id"])
                    await rt.emit(
                        "tool.started",
                        turn,
                        {"tool_call_id": call.id, "name": call.name, "arguments": call.arguments},
                    )

                async def completed(call, result):
                    nonlocal failures, tool_count
                    # Persist each completed observation immediately, even if another safe read is still running.
                    observation = await rt.store.run(
                        "commit_tool", session, turn, by_call[call.id]["id"], result
                    )
                    observations[call.id] = observation
                    tool_count += 1
                    failures += int(result.failed)
                    if call.name == "kaki_exec" and rt.is_test_command(call.arguments.get("command", "")):
                        evidence = {
                            "command": call.arguments["command"],
                            "cwd": call.arguments.get("working_dir", str(rt.workspace.root)),
                            "exit_code": result.exit_code,
                            "artifacts": result.artifacts,
                            "run_id": by_call[call.id]["id"],
                        }
                        verification["commands"].append(evidence)
                        verification["status"] = (
                            "passed" if result.exit_code == 0 and not result.failed else "failed"
                        )
                    await rt.emit(
                        "tool.finished",
                        turn,
                        {
                            "tool_call_id": call.id,
                            "name": call.name,
                            "failed": result.failed,
                            "preview": result.text[:600],
                            "artifacts": result.artifacts,
                            "exit_code": result.exit_code,
                        },
                    )

                results = await rt.tools.execute_many(response.tool_calls, ctx, started, completed)
                for call, result in zip(response.tool_calls, results, strict=True):
                    window.messages.append(observations[call.id])
                    if result.failed and not any(
                        t in result.text.lower() for t in ["timeout", "timed out", "429", "rate limit"]
                    ):
                        hard_count = hard_count + 1 if hard_name == call.name else 1
                        hard_name = call.name
                    else:
                        hard_count = 0
                        hard_name = None
                    if hard_count >= 2 and hard_nudges < 2:
                        from dataclasses import replace

                        window.messages[-1] = replace(
                            window.messages[-1],
                            content=window.messages[-1].content
                            + "\n[Kaki] 同名工具连续硬失败，请分析原因并更换方法。",
                        )
                        hard_count = 0
                        hard_nudges += 1
                previous_tools = True
                continue
            if response.finish_reason != "completed":
                if response.text or response.native_items:
                    msg, _ = await rt.store.run("commit_model", session, turn, response)
                    window.messages.append(msg)
                    await rt.emit(
                        "model.committed",
                        turn,
                        {"draft_id": response.call_id, "message_id": msg.id, "text": response.text},
                    )
                return TurnResult(
                    turn,
                    "interrupted",
                    response.text or "模型输出达到上限，本次任务未完整完成。",
                    "output_limit",
                    tool_count,
                    failures,
                    verification,
                )
            if not response.text.strip():
                if not cfg.empty_recovery_enabled:
                    raise ProviderError("模型未返回正文或工具调用", "empty_response")
                thinking = any(x.get("type") == "reasoning" for x in response.native_items)
                if thinking and prefills < cfg.thinking_prefill_max_retries:
                    prefills += 1
                    msg, _ = await rt.store.run("commit_model", session, turn, response)
                    window.messages.append(msg)
                    previous_tools = False
                    continue
                if previous_tools and nudges < cfg.post_tool_empty_max_nudges:
                    nudges += 1
                    window.messages.append(
                        Message("user", "根据已有工具结果继续完成任务，或给出明确结果；不要返回空正文。")
                    )
                    previous_tools = False
                    continue
                if empty_retries < cfg.empty_content_max_retries:
                    empty_retries += 1
                    previous_tools = False
                    continue
                raise ProviderError("模型空响应恢复次数已用尽；任务未被标为成功", "empty_response")
            msg, _ = await rt.store.run("commit_model", session, turn, response)
            await rt.emit(
                "model.committed",
                turn,
                {"draft_id": response.call_id, "message_id": msg.id, "text": response.text},
            )
            return TurnResult(
                turn, "completed", response.text, "final_answer", tool_count, failures, verification
            )
        await rt.emit("turn.finalizing", turn, {"reason": "iteration_limit"})
        prompt = Message(
            "user",
            "本轮迭代预算已耗尽。工具已关闭。仅根据已有真实结果总结完成项、验证证据和未完成项，不要声称任务已全部完成。使用用户语言。",
        )
        text = "本轮达到迭代上限，已保存确定的执行记录；请检查文件与未完成事项后继续。"
        try:
            window = await rt.context.rebalance(session, window, definitions)
            response = await rt.provider.complete(
                [*window.messages, prompt],
                None,
                max_tokens=min(2048, cfg.max_tokens),
                turn_id=turn,
                kind="finalize",
                on_delta=on_delta,
            )
            if response.text and not response.tool_calls:
                text = response.text
            else:
                response = ModelResponse(text=text)
        except asyncio.CancelledError:
            raise
        except Exception:
            response = ModelResponse(text=text)
        msg, _ = await rt.store.run("commit_model", session, turn, response)
        await rt.emit(
            "model.committed", turn, {"draft_id": response.call_id, "message_id": msg.id, "text": text}
        )
        return TurnResult(turn, "interrupted", text, "iteration_limit", tool_count, failures, verification)
