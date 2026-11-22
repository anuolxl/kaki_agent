import asyncio

import httpx
import pytest
from conftest import ScriptedProvider

from kaki.app_controller import AppController
from kaki.app_server import AppServer
from kaki.runtime import Runtime
from kaki.tui import KakiApp
from kaki.types import ModelResponse, ToolCall


async def eventually(predicate, timeout=5):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


async def test_tui_invalid_approval_keeps_question_then_executes(workspace, config):
    model = ScriptedProvider(
        ModelResponse(
            tool_calls=[ToolCall("write", "kaki_write_file", {"path": "ok.txt", "content": "approved"})]
        ),
        ModelResponse(text="已按批准写入"),
    )
    rt = Runtime(workspace, config, provider=model)
    ui = KakiApp(rt)
    async with ui.run_test(size=(120, 40)) as pilot:
        await asyncio.wait_for(ui.ready_event.wait(), 5)
        await ui.submit_text("看看仓库")
        await eventually(lambda: bool(ui.question_id))
        question_id = ui.question_id
        await ui.submit_text("允许，但不要动其他文件")
        assert ui.question_id == question_id and not (workspace.root / "ok.txt").exists()
        await ui.submit_text("仅允许这一次")
        await asyncio.wait_for(ui.turn_done.wait(), 5)
        assert ui.last_result["status"] == "completed"
        assert (workspace.root / "ok.txt").read_text() == "approved"
        await pilot.pause()


@pytest.mark.asyncio
async def test_app_http_scoped_approval_and_persistent_denied_card(workspace, config):
    model = ScriptedProvider(
        ModelResponse(
            tool_calls=[ToolCall("write", "kaki_write_file", {"path": "denied.txt", "content": "bad"})]
        ),
        ModelResponse(text="用户拒绝，未执行"),
    )
    controller = AppController(config, runtime_factory=lambda w, c: Runtime(w, c, provider=model))
    server = await AppServer(controller, initial_path=workspace.root).start()
    try:
        headers = {"Authorization": "Bearer " + server.secret, "Origin": server.origin, "X-Kaki-Client": "1"}
        async with httpx.AsyncClient(base_url=server.origin, headers=headers, trust_env=False) as client:
            e = controller.engine(workspace.id)
            path = f"/api/workspaces/{workspace.id}/sessions/{e.session}"
            assert (await client.post(path + "/send", json={"text": "看看仓库"})).status_code == 202
            await eventually(lambda: e.phase == "waiting_user")
            detail = (await client.get(path)).json()
            q = detail["live"]["question"]
            assert q["kind"] == "approval" and q["turn_id"] == e.runtime.active_turn
            assert "future" not in q and q["operation"]["resource"].endswith("denied.txt")
            other = await controller.new_session(workspace.id)
            response = await client.post(
                f"/api/workspaces/{workspace.id}/sessions/{other}/answer",
                json={"question_id": q["id"], "text": "拒绝执行", "choice": "deny"},
            )
            assert response.status_code == 409
            assert (
                await client.post(
                    path + "/answer", json={"question_id": q["id"], "text": "拒绝执行", "choice": "deny"}
                )
            ).status_code == 200
            await e.task
            detail = (await client.get(path)).json()
            assert not detail["live"]["busy"] and detail["live"]["phase"] == "completed"
            assert detail["turns"][-1]["tools"][0]["state"] == "denied"
            assert not (workspace.root / "denied.txt").exists()
    finally:
        await server.close()
