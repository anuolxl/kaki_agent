# Kaki development contract

Kaki is a Python 3.12 local desktop/CLI/TUI coding agent. It owns its package and state, not a production service. Design source: docs/architecture.md, docs/module-spec/core.md and docs/module-spec/app.md.

- Keep TUI, orchestration, model transport, context selection, tools, storage and file snapshots separate.
- Desktop UI state is a projection. Do not report completion before Runtime finishes journal/snapshot work. Disconnect means unknown, never completed. Stop cancels one Turn; Ctrl+C and closing the native window exit the process.
- Keep existing camelCase/snake_case configuration keys; skip empty environment overrides. Secrets must never enter logs, screenshots or Git.
- Preserve raw session facts. Context compaction selects/archives facts; it must not delete the source history or split tool-call groups.
- Commit complete model output before executing tools; record execution intent before side effects. On crash, unresolved side effects are UNKNOWN, never automatically replayed.
- A file snapshot is not a transaction for browser or network operations. Never modify the user's Git index/branch when creating snapshots.
- Every persisted field and public interface change must update docs/module-spec/core.md and meaningful behavior tests.
- No recursive agents or production deployment in this project.

## Local verification capability

Local compile: yes, macOS/Linux with Python 3.12: `uv run python -m compileall -q src`.
Local tests: yes: `uv run pytest`; lint/format: `uv run ruff check .` and `uv run ruff format --check .`. Frontend state tests: Node 20+ `node --test tests/ui-state.test.mjs`; syntax: `node --check src/kaki/ui/app.mjs`. Native window verification requires macOS and pywebview/WebKit; Linux CI tests the same HTTP/WebSocket backend without a native window.
Live tests require an explicitly configured Right Code key; browser integration requires Node and Playwright MCP. Tests use temporary Git repositories and isolated KAKI_HOME directories. Do not modify media_platform to prove Kaki works. media_platform tests must follow its own AGENTS.md (`make test`, macOS Stub mode).
