"""Stable installed entry: the target project never resolves Kaki's dependencies."""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
import shutil
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from kaki import __version__
from kaki.config import Config, config_path, load_config, normalize, safe_error, save_config
from kaki.runtime import Runtime
from kaki.types import KakiError, Message
from kaki.workspace import Workspace

app = typer.Typer(
    invoke_without_command=True,
    no_args_is_help=False,
    help="Kaki 本地编码Agent：单进程Python TUI、持久会话与有界上下文。",
)
console = Console()


@app.callback()
def entry(
    ctx: typer.Context,
    cwd: Path | None = typer.Option(None, "--cwd", "-C", help="目标单Git仓库，默认当前目录"),
    config: Path | None = typer.Option(None, "--config", help="配置文件，默认~/.kaki/config.json"),
    resume: str | None = typer.Option(None, "--resume", help="恢复会话ID或last，不自动重放未知操作"),
    model: str | None = typer.Option(None, "--model"),
    version: bool = typer.Option(False, "--version", is_eager=True),
):
    if version:
        console.print("Kaki Agent " + __version__)
        raise typer.Exit()
    ctx.obj = {"cwd": cwd, "config": config, "resume": resume, "model": model}
    if ctx.invoked_subcommand is not None:
        return
    options = load_config(config, model=model)
    options.validate_credentials()
    workspace = Workspace.resolve(cwd, options)
    from kaki.tui import KakiApp

    KakiApp(Runtime(workspace, options), resume=resume).run()


@app.command()
def setup(
    ctx: typer.Context,
    import_config: Path | None = typer.Option(
        None, "--import-config", help="从已有兼容配置安全复制OpenAI字段"
    ),
    browser: bool = typer.Option(True, "--browser/--no-browser"),
):
    target = config_path(ctx.obj["config"])
    options = load_config(target) if target.exists() else Config()
    data = options.model_dump(mode="json")
    data["providers"]["openai"]["api_key"] = options.providers.openai.api_key.get_secret_value()
    if import_config:
        raw = normalize(json.loads(import_config.expanduser().read_text()))
        source = raw.get("providers", {}).get("openai", {})
        data["providers"]["openai"] = {
            k: v for k, v in source.items() if k in {"api_base", "api_key", "extra_headers", "models"}
        }
        prior = raw.get("agents", {}).get("defaults", {})
        for k in ["model", "max_tokens", "context_window_tokens", "max_tool_iterations", "temperature"]:
            if k in prior:
                data["agents"]["defaults"][k] = prior[k]
    if not data["providers"]["openai"].get("api_key"):
        data["providers"]["openai"]["api_key"] = typer.prompt(
            "Right Code API Key（只写入本机配置）", hide_input=True
        )
    data["agents"]["defaults"]["provider"] = "openai"
    data["memory"]["backend"] = "local"
    if browser:
        data["tools"]["mcp_servers"] = {
            "browser": {
                "type": "stdio",
                "command": "npx",
                "args": ["-y", "@playwright/mcp@0.0.85", "--browser", "chrome"],
                "tool_timeout": 45,
            }
        }
    else:
        data["tools"]["mcp_servers"] = {}
    options = Config.model_validate(data)
    path = save_config(options, target)
    console.print(f"[green]已保存[/green] {path}（权限0600；API Key不显示）")
    console.print(
        f"model={options.agents.defaults.model} · base={options.providers.openai.api_base} · browser={browser}"
    )


def environment_report(options: Config, workspace: Workspace, chosen_config: Path | None = None) -> dict:
    packages = {
        p: importlib.metadata.version(p) for p in ["kaki-agent", "openai", "textual", "mcp", "tiktoken"]
    }
    binaries = {p: shutil.which(p) for p in ["git", "rg", "node", "npx"]}
    return {
        "version": __version__,
        "workspace": str(workspace.root),
        "state": str(workspace.state),
        "config": str(config_path(chosen_config)),
        "model": options.agents.defaults.model,
        "api_base": options.providers.openai.api_base,
        "api_key_configured": bool(options.providers.openai.api_key.get_secret_value()),
        "context_window_cap": options.agents.defaults.context_window_tokens,
        "binaries": binaries,
        "packages": packages,
        "protocol": "responses",
        "browser_configured": bool(options.tools.mcp_servers),
    }


@app.command()
def doctor(
    ctx: typer.Context,
    probe: bool = typer.Option(False, "--probe", help="发起真实模型原生工具调用与回传"),
    browser: bool = typer.Option(False, "--browser", help="验证浏览器MCP及about:blank导航"),
    report: Path | None = typer.Option(None, "--report", help="保存不含密钥的验证JSON"),
):
    options = load_config(ctx.obj["config"], model=ctx.obj["model"])
    workspace = Workspace.resolve(ctx.obj["cwd"], options)
    result = environment_report(options, workspace, ctx.obj["config"])

    async def verify():
        from kaki.provider import RightCodeProvider

        if probe:
            provider = RightCodeProvider(options)
            try:
                tools = [
                    {
                        "type": "function",
                        "name": "kaki_probe",
                        "description": "Return the verification marker.",
                        "parameters": {
                            "type": "object",
                            "properties": {"value": {"type": "string"}},
                            "required": ["value"],
                            "additionalProperties": False,
                        },
                        "strict": False,
                    }
                ]
                messages = [
                    Message(
                        "system",
                        "You are Kaki. Call kaki_probe with value KAKI_READY. After the tool result, return only its text.",
                    ),
                    Message("user", "Verify Kaki tools."),
                ]
                first = await provider.complete(
                    messages, tools, max_tokens=2048, tool_choice={"type": "function", "name": "kaki_probe"}
                )
                if not first.tool_calls:
                    raise KakiError("模型没有返回预期原生工具调用")
                messages.append(
                    Message(
                        "assistant",
                        first.text,
                        tool_calls=[
                            {"id": c.id, "name": c.name, "arguments": c.arguments} for c in first.tool_calls
                        ],
                        native_items=first.native_items,
                    )
                )
                for call in first.tool_calls:
                    messages.append(Message("tool", "KAKI_READY", tool_call_id=call.id))
                second = await provider.complete(messages, tools, max_tokens=2048)
                if "KAKI_READY" not in second.text:
                    raise KakiError("工具结果回传验证失败")
                result["probe"] = {
                    "ok": True,
                    "tool_round_trip": True,
                    "response": second.text,
                    "usage": [first.usage, second.usage],
                }
            finally:
                await provider.close()
        if browser:
            from kaki.browser_mcp import BrowserMcp

            client = BrowserMcp(workspace, options)
            try:
                tools = await client.start()
                navigation = await client.call("kaki_browser_navigate", {"url": "about:blank"})
                if navigation.failed:
                    raise KakiError(navigation.text)
                result["browser"] = {"ok": True, "tools": len(tools), "navigation": navigation.text[:500]}
            finally:
                await client.close()

    asyncio.run(verify())
    required = ["git", "rg"] + (["node", "npx"] if options.tools.mcp_servers else [])
    result["ok"] = all(result["binaries"][x] for x in required) and result["api_key_configured"]
    if report:
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    console.print_json(data=result)
    if not result["ok"]:
        raise typer.Exit(1)


@app.command("run")
def run_once(
    ctx: typer.Context,
    message: str = typer.Option(..., "--message", "-m"),
    json_output: bool = typer.Option(False, "--json"),
):
    """Headless automation uses exactly the same Runtime as TUI."""
    options = load_config(ctx.obj["config"], model=ctx.obj["model"])
    workspace = Workspace.resolve(ctx.obj["cwd"], options)

    async def go():
        rt = Runtime(workspace, options, interactive=False)

        async def event(e):
            if json_output:
                console.print_json(data={"type": e.type, "turn_id": e.turn_id, "data": e.data})
            elif e.type == "model.delta":
                console.print(e.data["text"], end="", markup=False, highlight=False)
            elif e.type == "tool.started":
                console.print("\n[dim]工具 " + e.data["name"] + "[/dim]")
            elif e.type == "question.requested":
                answer = await asyncio.to_thread(input, "\n" + e.data["question"] + "\n> ")
                await rt.answer(e.data["question_id"], answer)

        rt.listeners.append(event)
        try:
            await rt.initialize(ctx.obj["resume"])
            result = await rt.run(message)
            if not json_output:
                console.print(
                    f"\n\n状态={result.status} 原因={result.stop_reason} Token={result.usage.get('total_tokens', 0)} 会话={rt.session_id}"
                )
            return result.status
        finally:
            await rt.close()

    status = asyncio.run(go())
    if status == "failed":
        raise typer.Exit(1)


@app.command()
def sessions(ctx: typer.Context):
    options = load_config(ctx.obj["config"])
    ws = Workspace.resolve(ctx.obj["cwd"], options)
    from kaki.store import Store

    async def read():
        store = Store(ws.state / "kaki.sqlite3", ws.id)
        await store.open()
        try:
            return await store.run("sessions")
        finally:
            await store.close()

    table = Table("Session", "标题", "更新时间")
    for s in asyncio.run(read()):
        table.add_row(s["id"], s["title"], s["updated"])
    console.print(table)


def main():
    try:
        app()
    except (KakiError, ValueError, OSError) as exc:
        console.print("[red]Kaki: " + safe_error(exc) + "[/red]", markup=True)
        raise SystemExit(1) from None
