"""The bounded model -> tool -> observation loop; Runtime alone publishes terminal state."""

from __future__ import annotations

import asyncio
import shlex
import time

from kaki.types import Message, ModelResponse, ProviderError, TurnResult


def verification_status(commands):
    # 按 cwd 与规范 argv 分组取最近结果；另一套件通过不能覆盖尚未解决的失败。
    latest = {}
    for command in commands:
        key = (command["cwd"], tuple(shlex.split(command["command"])))
        latest[key] = command
    if not latest:
        return "not_run"
    return (
        "passed"
        if all(x["state"] == "succeeded" and x["exit_code"] == 0 for x in latest.values())
        else "failed"
    )


def verification_evidence(runtime, call, result, run_id):
    # 只有真正执行的测试入口生成证据；外部测试平台提交成功不属于测试通过。
    if not result.executed:
        return None
    command, cwd = None, str(runtime.workspace.root)
    if call.name == "kaki_exec" and runtime.is_test_command(call.arguments.get("command", "")):
        command = call.arguments["command"]
        cwd = str(runtime.workspace.path(call.arguments.get("working_dir", ".")))
    if call.name == "kaki_command":
        profile = runtime.config.tools.commands.get(call.arguments.get("name"))
        if profile and profile.capability == "test.execute":
            expand = runtime.tools.development.expand
            command = shlex.join([expand(v) for v in profile.argv + call.arguments.get("arguments", [])])
            cwd = expand(profile.cwd or "{workspace}")
    if not command:
        return None
    return {
        "command": command,
        "cwd": cwd,
        "exit_code": result.exit_code,
        "executed": result.executed,
        "state": "unknown"
        if result.uncertain
        else "denied"
        if result.denied
        else "failed"
        if result.failed
        else "succeeded",
        "artifacts": result.artifacts,
        "run_id": run_id,
    }


class AgentLoop:
    # 有界的模型—工具—观察循环；它产生执行结果，Runtime 才负责发布完整终态。
    def __init__(self, runtime):
        self.runtime = runtime

    async def run(
        self,
        turn: str,
        session: str,
        user: Message,
        history: list[Message],
        recovery: str = "",
        request=None,
        before_mutation=None,
    ) -> TurnResult:
        # 每轮先复核窗口，再调模型；完整模型输出提交后才允许任何工具副作用。
        rt = self.runtime
        cfg = rt.config.agents.defaults
        # 工具 Schema 也占输入预算；Skill/发现激活后，下次请求要重新计入。
        definitions = rt.tools.definitions()
        # 接单时选择历史/规则/Skill并验证窗口；选择不会删除 Store 的原文。
        window = await rt.context.assemble(
            session, history, user, definitions, recovery=recovery, request=request
        )
        for required in window.metadata.get("skill_required_tools", {}).values():
            rt.tools.activate(required)
        await rt.emit(
            "context.assembled", turn, window.metadata | {"estimated_tokens": window.estimated_tokens}
        )
        # 工具观察统计与主循环迭代数分开；终态还会从已提交日志重建。
        failures = 0
        tool_count = 0
        # 各恢复预算独立且有限，避免一次空响应使流程无限重试。
        empty_retries = 0
        prefills = 0
        nudges = 0
        previous_tools = False
        # 上游真实溢出的应急次数，不是正常历史选择的次数。
        compressions = 0
        hard_name = None
        hard_count = 0
        hard_nudges = 0
        # 保存实际执行证据，不能由最终自然语言总结推断通过。
        verification = {"status": "not_run", "commands": []}
        # 执行上下文同时携带原文和派生理解，权限只使用 human_text。
        ctx = {
            "store": rt.store,
            "session_id": session,
            "turn_id": turn,
            "user_message_id": user.id,
            "user_text": user.content,
            "human_text": request.human_text if request else user.content,
            "request": request,
            "before_mutation": before_mutation,
        }
        draft_last = 0.0
        draft_text = ""
        # 分支切换后重新读取规则与检索提示，同批后续动作暂不执行。
        refresh_context = False

        async def on_delta(text, call_id):
            # delta 是展示草稿；定期保存用于崩溃诊断，不能据此提前执行工具参数。
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
                # 等待完整响应，流式 token 只是 UI 草稿，不能作为执行指令。
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
                # 完整 assistant 输出与所有 planned 调用先落库，关闭“执行了却没有意图记录”的窗口。
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
                # 上游 call_id 与内部 run_id 的映射只用于这一批工具，不可与 Session/Turn ID 混用。
                by_call = {r["tool_call_id"]: r for r in runs}
                observations = {}

                async def started(call):
                    # 实际 I/O 前把工具 run 标记为 running，崩溃后才有依据判断结果未知。
                    await rt.store.run("mark_running", by_call[call.id]["id"])
                    await rt.emit(
                        "tool.started",
                        turn,
                        {"tool_call_id": call.id, "name": call.name, "arguments": call.arguments},
                    )

                async def completed(call, result):
                    # 每个工具一完成就配对落库；同批其他读任务失败不能抹掉已确定的观察。
                    nonlocal failures, tool_count, refresh_context
                    # Persist each completed observation immediately, even if another safe read is still running.
                    observation = await rt.store.run(
                        "commit_tool", session, turn, by_call[call.id]["id"], result
                    )
                    observations[call.id] = observation
                    tool_count += 1
                    failures += int(result.failed)
                    refresh_context = refresh_context or result.context_refresh
                    evidence = verification_evidence(rt, call, result, by_call[call.id]["id"])
                    if evidence:
                        verification["commands"].append(evidence)
                        verification["status"] = verification_status(verification["commands"])
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
                            "executed": result.executed,
                            "state": "unknown"
                            if result.uncertain
                            else "denied"
                            if result.denied
                            else "failed"
                            if result.failed
                            else "succeeded",
                        },
                    )

                # 每项观察立即提交；允许安全读并发，其余副作用串行。
                results = await rt.tools.execute_many(response.tool_calls, ctx, started, completed)
                for call, result in zip(response.tool_calls, results, strict=True):
                    window.messages.append(observations[call.id])
                    if result.denied:
                        from dataclasses import replace

                        window.messages[-1] = replace(
                            window.messages[-1],
                            content=window.messages[-1].content
                            + "\n[Kaki] 此操作被权限策略拒绝。不得通过其他工具绕过，不得声称已经执行或验证。",
                        )
                        continue
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
                # 切分支后规则可能已变：先重读前缀与Skill，再进入下一轮模型判断。
                if refresh_context:
                    if request:
                        request.rewrite.queries["code"] = [request.original_text]
                    system, metadata = await rt.context.prefix(user, recovery, request)
                    window.messages[0] = system
                    window.metadata.update(metadata, workspace_context_refreshed=True)
                    refresh_context = False
                    await rt.emit(
                        "context.compacted",
                        turn,
                        {"reason": "git_branch_changed", "text": "分支变化后已重新读取仓库规则与Skill"},
                    )
                previous_tools = True
                continue
            # 输出被截断时保留已知正文并中断，不能把半个响应当完成。
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
            # 空内容恢复有独立小预算；失败明确结束，不能进入无限空循环。
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
        # 主迭代用尽后关闭工具，只允许根据已有事实总结，不继续制造副作用。
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
