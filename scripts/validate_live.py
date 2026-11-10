"""Run the actual Textual app with real Right Code/MCP; save native SVG evidence.

This intentionally targets only the named disposable pricing fixture, never a business repository.
Usage: uv run python scripts/validate_live.py --workspace /private/tmp/kaki-e2e-workspace --output /private/tmp/kaki-evidence
"""

import argparse
import asyncio
import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from kaki.config import load_config
from kaki.runtime import Runtime
from kaki.tui import KakiApp
from kaki.workspace import Workspace


async def validate(args):
    root = args.workspace.resolve()
    if root.name != "kaki-e2e-workspace" or "disposable" not in (root / "AGENTS.md").read_text():
        raise SystemExit("Only the disposable kaki-e2e-workspace fixture is accepted")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    tests_hash = hashlib.sha256((root / "test_pricing.py").read_bytes()).hexdigest()
    report = {"kind": "actual_textual_app_real_right_code", "workspace": str(root), "turns": []}
    events_file = (output / "events.jsonl").open("w")
    cfg = load_config()
    cfg.validate_credentials()
    runtime = Runtime(Workspace.resolve(root, cfg), cfg)

    async def record(e):
        events_file.write(json.dumps(asdict(e), ensure_ascii=False) + "\n")
        events_file.flush()

    runtime.listeners.append(record)
    ui = KakiApp(runtime)
    tasks = [
        (
            "01-travel",
            "请做一份上海秋天两日旅游攻略：地铁和步行，预算600元不含住宿，兼顾梧桐街区和滨江，包含下雨备选。请用浏览器访问上海市文旅局官方网站 https://whlyj.sh.gov.cn/ 核对资料，至多3次浏览器工具调用；不确定的票价和营业时间请说明待核实。600字以内。只回答，不要修改本地文件。",
        ),
        (
            "02-followup",
            "沿用刚才的600元预算和路线，第二天改成适合带爸妈的慢节奏，给出3条具体调整。不要重新搜索，不修改文件。150字以内。",
        ),
        (
            "03-coding",
            "现在处理本工作区的代码任务。请读取AGENTS.md、pricing.py和test_pricing.py，先运行python3 -m unittest -v观察失败，再只修改pricing.py：discounted_price应返回打折后的应付金额，并对0到100之外的percent抛ValueError。不要修改测试。修改后再次运行同一个测试命令，最后报告结果。",
        ),
        ("04-compact", "/compact"),
    ]
    try:
        async with ui.run_test(size=(145, 52)) as pilot:
            await asyncio.wait_for(ui.ready_event.wait(), 100)
            if not runtime.initialized:
                raise AssertionError("TUI initialization failed")
            report["session_id"] = runtime.session_id
            await pilot.pause()
            await ui.save_evidence(output / "00-ready.svg")
            print("TUI_READY", flush=True)
            for name, text in tasks:
                field = ui.query_one("#prompt")
                field.value = text
                await pilot.press("enter")
                await ui.wait_for_turn(360)
                await pilot.pause()
                result = ui.last_result
                await ui.save_evidence(output / (name + ".svg"))
                report["turns"].append({"name": name, "prompt": text, "result": result})
                (output / "live-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
                print(
                    name, result["status"], result["stop_reason"], "tools=" + str(result["tools"]), flush=True
                )
                if result["status"] != "completed":
                    raise AssertionError(result.get("error") or result["text"])
            report["context"] = runtime.context.last_metadata
            report["tests_unchanged"] = (
                hashlib.sha256((root / "test_pricing.py").read_bytes()).hexdigest() == tests_hash
            )
            assert report["tests_unchanged"]
            coding = report["turns"][2]["result"]
            assert coding["verification"]["status"] == "passed", coding
            report["snapshots"] = await runtime.store.run("snapshots", coding["turn_id"])
            assert all(row["state"] == "ready" for row in report["snapshots"])
        # A new process-equivalent Runtime reopens the actual SQLite session; no model call is needed to render it.
        restored = Runtime(Workspace.resolve(root, cfg), cfg)
        restored_ui = KakiApp(restored, resume=report["session_id"])
        async with restored_ui.run_test(size=(145, 52)) as pilot:
            await asyncio.wait_for(restored_ui.ready_event.wait(), 100)
            await pilot.pause()
            messages = await restored.store.run("messages", restored.session_id)
            assert any("上海" in m.content for m in messages if m.role == "assistant")
            await restored_ui.save_evidence(output / "05-resumed.svg")
            report["resume_messages"] = len(messages)
        report["ok"] = True
    finally:
        (output / "live-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
        events_file.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(validate(args))


if __name__ == "__main__":
    main()
