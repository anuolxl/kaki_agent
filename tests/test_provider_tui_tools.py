import asyncio
import json

import httpx
import pytest
from conftest import ScriptedProvider
from openai import AsyncOpenAI

from kaki.provider import RightCodeProvider, normalize_response, wire_input
from kaki.runtime import Runtime
from kaki.tui import ChatMessage, KakiApp
from kaki.types import Message, ModelResponse, ProviderError, ToolCall


def test_native_reasoning_and_function_call_replay():
    native = [
        {"id": "rs", "type": "reasoning", "encrypted_content": "opaque"},
        {"type": "function_call", "call_id": "c1", "name": "read", "arguments": '{"path":"x"}'},
    ]
    response = normalize_response(
        {"status": "completed", "output": native, "usage": {"input_tokens": 11, "output_tokens": 7}}, "local"
    )
    assert response.tool_calls[0].id == "c1"
    instructions, inputs = wire_input(
        [
            Message("system", "rules"),
            Message("user", "task"),
            Message("assistant", native_items=native),
            Message("tool", "real observation", tool_call_id="c1"),
        ]
    )
    assert instructions == "rules"
    assert inputs[1:3] == native
    assert inputs[-1] == {"type": "function_call_output", "call_id": "c1", "output": "real observation"}
    assert response.usage["total_tokens"] == 18


@pytest.mark.asyncio
@pytest.mark.parametrize("complete", [True, False])
async def test_stream_needs_terminal_event_before_tools(config, complete):
    config.providers.openai.api_key = __import__("pydantic").SecretStr("test")
    records = []
    requests = []

    async def record(value):
        records.append(dict(value))

    async def transport(request):
        requests.append(json.loads(request.content))
        events = [
            {
                "type": "response.function_call_arguments.delta",
                "item_id": "fc",
                "output_index": 0,
                "delta": '{"path":',
            }
        ]
        if complete:
            events.append(
                {
                    "type": "response.completed",
                    "response": {
                        "id": "resp",
                        "status": "completed",
                        "output": [
                            {
                                "type": "function_call",
                                "call_id": "call-1",
                                "name": "read",
                                "arguments": '{"path":"file.py"}',
                            }
                        ],
                        "usage": {"input_tokens": 10, "output_tokens": 3},
                        "model": "model",
                    },
                }
            )
        body = "".join("data: " + json.dumps(e) + "\n\n" for e in events)
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    provider = RightCodeProvider(config, record)
    await provider.client.close()
    provider.client = AsyncOpenAI(
        api_key="test",
        base_url="https://mock.test/v1",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport)),
    )
    try:
        if complete:
            result = await provider.complete([Message("user", "task")])
            assert result.tool_calls[0].arguments == {"path": "file.py"}
            assert records[-1]["status"] == "completed"
        else:
            with pytest.raises(ProviderError, match="完整结束事件"):
                await provider.complete([Message("user", "task")])
            assert records[-1]["status"] == "failed"
        assert requests[0]["max_output_tokens"] == config.agents.defaults.max_tokens
        assert requests[0]["store"] is False
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_tool_guards_timeout_and_failed_test(workspace, config):
    rt = Runtime(workspace, config, provider=ScriptedProvider())
    try:
        await rt.initialize()
        file = workspace.root / "a.py"
        file.write_text("same\nsame\n")
        bad = await rt.tools.execute(
            ToolCall("e", "kaki_edit_file", {"path": "a.py", "old_text": "same", "new_text": "new"}), {}
        )
        assert bad.failed and "不唯一" in bad.text
        changed = await rt.tools.execute(
            ToolCall("e2", "kaki_write_file", {"path": "a.py", "content": "new", "expected_hash": "wrong"}),
            {},
        )
        assert changed.failed and file.read_text() == "same\nsame\n"
        denied = await rt.tools.execute(ToolCall("x", "kaki_exec", {"command": "git push origin master"}), {})
        assert denied.failed
        timeout = await rt.tools.execute(
            ToolCall("s", "kaki_exec", {"command": "sleep 10", "timeout": 1}), {}
        )
        assert timeout.failed
        schema = await rt.tools.execute(ToolCall("z", "kaki_read_file", {"path": "a.py", "limit": -5}), {})
        assert schema.failed
    finally:
        await rt.close()


@pytest.mark.asyncio
async def test_tui_stream_upsert_multiturn_and_resume(workspace, config):
    model = ScriptedProvider(ModelResponse(text="上海秋天散步"), ModelResponse(text="保留上一轮的上海路线"))
    rt = Runtime(workspace, config, provider=model)
    ui = KakiApp(rt)
    async with ui.run_test(size=(120, 40)) as pilot:
        await asyncio.wait_for(ui.ready_event.wait(), 5)
        assert rt.initialized
        field = ui.query_one("#prompt")
        field.value = "上海秋天去哪里？"
        await pilot.press("enter")
        await ui.wait_for_turn(5)
        await pilot.pause()
        assert ui.last_result["status"] == "completed"
        assert len(ui.query(ChatMessage)) == 2
        field.value = "按上面的路线给我第二天"
        await pilot.press("enter")
        await ui.wait_for_turn(5)
        await pilot.pause()
        assert len(ui.query(ChatMessage)) == 4
        assert any("上海秋天散步" in m.content for m in model.calls[-1][0])
        await ui.submit_text("/new")
        assert len(ui.query(ChatMessage)) == 0
        sessions = await rt.store.run("sessions")
        old = next(s for s in sessions if s["id"] != rt.session_id)
        await ui.submit_text("/resume " + old["id"])
        assert len(ui.query(ChatMessage)) == 4


def test_export_preserves_cjk_terminal_cells():
    import xml.etree.ElementTree as ET

    from kaki.tui import terminal_svg

    source = (
        '<svg xmlns="http://www.w3.org/2000/svg"><text x="0" y="20" textLength="48.8">中文ab</text></svg>'
    )
    node = next(ET.fromstring(terminal_svg(source)).iter("{http://www.w3.org/2000/svg}text"))
    assert node.text == "中文ab"
    assert float(node.attrib["textLength"]) == pytest.approx(73.2)
