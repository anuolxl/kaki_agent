# Development tools and local Skills (v0.4)

This module turns previously unavailable development operations into registered, executable capabilities. A user approval cannot install a tool. Raw `bash` remains outside `kaki_exec`; administrator-configured command profiles execute trusted scripts through a separate entry point. These are application checks, not an OS sandbox.

## Ownership and call order

`Runtime.initialize → ToolRegistry → DevelopmentTools`. Each Turn resets deferred-tool activation and permission grants. `ContextEngine.prefix → Skills.resolve` loads metadata and at most the configured body budget. Ready selected Skills activate required deferred tools. `AgentLoop → ToolRegistry.prepare → registered preflight → PermissionService.authorize → recheck → snapshot when needed → journal running → execute → Store.commit_tool → paired observation → next model request`.

`execution.run_process(argv, cwd, timeout, env)` uses argv without a shell, a filtered inherited environment, bounded output and process-group cancellation. Trusted profiles can themselves run Bash/CLI/repository scripts and have their inherent privileges. Tests and Git hooks can execute repository code. Declaring a command `workspace.check` is an administrator trust decision, not a proof of harmlessness.

## Configuration contract

Both camelCase and snake_case keys are accepted; existing provider settings and nonempty environment precedence stay unchanged. `files` is an opaque path→SHA256 mapping, like opaque `env`/`extra_headers` keys.

| Field | Default / constraint | Consumer |
|---|---|---|
| tools.development.enabled | false | Registers kaki_git / kaki_mr and guards file mutations on protected branches |
| protectedBranches | master, main, release/* | fnmatch against local branch; detached HEAD denied for mutations |
| allowPush / allowMrCreate | false / false | Ability ceiling, not human approval |
| tools.commands.<name>.argv | required nonempty string list | Fixed executable and argument prefix; no shell evaluation |
| description / cwd / timeout | required / workspace / 120; timeout 1–3600 | Discovery and execution |
| capability | process.execute | workspace.check, test.execute, process.execute, external.read, external.write |
| allowArguments / allowedFlags | false / [] | Explicit allowed suffix; no arbitrary unregistered command |
| pathFlags / writeFlags | [] / [] | Input under workspace/artifacts/workflows; output only state/workflows |
| files / env | {} / {} | Dependency SHA256 pins / trusted non-model environment |
| tools.discovery.enabled / maxResults | true / 5, 1–10 | Deferred schema loading |
| skillForge.catalogMax | 40, 1–200 | Metadata catalog budget |

Only `{workspace}` and `{state}` are substituted literally. A command profile is authored in trusted local config; the model cannot create or edit it through these tools. Input file content and pinned script hashes are rechecked after approval. Changed dependencies make the profile unavailable until its administrator reviews and updates the pin.

## Tools and interfaces

| Tool | Required parameters / actions | Effect |
|---|---|---|
| kaki_capabilities | none | Registered tools, schema visibility, profile integrity and Skill missing dependencies; CLI auth unproven until actual execution |
| kaki_tool_search | query; optional limit | BM25 + weighted RRF metadata ranking, returns definitions and activates deferred tools for this Turn |
| kaki_skill_search | query; optional limit | Ranked metadata/readiness; no automatic execution or authorization |
| kaki_artifact_write | text; format text/json/markdown | Immutable session input artifact, max 2 MiB, no source edits |
| kaki_command | name; arguments[] | Registered profile only; action is determined by trusted configuration |
| kaki_git | action status/diff/log/fetch/create_branch/switch/stage/commit/push | Explicit origin refs, clean tracked tree for switch; no reset/stash/force/delete/merge |
| kaki_mr | action prepare/find/create_draft; base; title/body for writes | GitHub/Codebase, prepare local artifact first; find/reuse existing MR and journal creation receipt |

Git fetch requires `ref`; create_branch requires `branch` and `base`; stage/commit require 1–100 exact file `paths`, never `.` or directories. Commit requires `message` and staged files exactly matching paths. Foreign staged files block the operation. Push targets the current nonprotected branch only, no force. The original `kaki_exec` test templates remain; `make test ARGS="..."` and one explicit `gofmt -w file.go` are supported.

Successful branch switch sets `ToolResult.context_refresh=true`. The remainder of that model's tool batch is answered with unexecuted failure observations; the loop rereads rules/Skills and clears derived code queries before asking the model again. Existing snapshot operations use a separate Git index; `.worktrees/` is excluded from snapshots to preserve unrelated worktrees.

## Data structures and persistence

| Structure / added field | Semantics / construction / compatibility |
|---|---|
| Tool.category='core', deferred=false, preflight=None | Registry metadata and trusted Python preparation callback; old Tool positional construction remains compatible |
| PreparedOperation.state_token=None | SHA256 over Git HEAD/branch/index/dirty or script/input content; preflight constructs, post-approval execution compares |
| PreparedOperation.review=None | Structured platform request target summary for approval: harnessTaskID/spaceNodeID/env/unit/xTtEnv/deploy/material links |
| ToolResult.uncertain=false | Ambiguous side effect result becomes durable tool_runs.state=unknown, never automatic replay |
| ToolResult.context_refresh=false | Successful branch-change invalidation; persisted in result_json with backward-compatible default |
| Skill row.required_tools/required_commands/private_paths | Metadata.kaki lists; all default []; dependency availability determines ready |
| Skill metadata.skill_required_tools/unavailable_skills | Active tool requirements and missing tools/commands retained in context plan for audit |
| Workspace.knowledge_roots | Existing field may hold a read-only directory OR exact inherited index.md file; no broad parent access |

SQLite remains schema3. Existing messages, decisions, tool_runs, context plan and events retain evidence. New optional ToolResult keys do not require a table migration. MR creation records a local exclusive pending receipt before remote I/O; unknown result only permits querying the same source/target, not another create. The remote service still needs its own idempotency guarantees.

## Skill selection and authorization

Roots, in priority order: `~/.kaki/skills`, project `.kaki/skills`, project `.agents/skills`; later roots override identical IDs. Symlinks may not escape each Skill root. Explicit `$name` and `/skill name` are matched against actual raw user text with an exact name boundary. Rewritten queries cannot forge explicit invocation. Ranking uses original query weight 2, derived query weight 1, BM25 per query, weighted RRF `weight/(60+rank)`, then ID deduplication. Body injection uses existing router topK/injectMax/token budget; a metadata catalog and search tools expose other entries without loading every body.

Readiness excludes missing tools or missing/unverified commands from automatic body injection and reports the exact missing dependencies. `kaki_skill_read` loads relative references on demand and activates declared tool dependencies; protected implementation files are denied. The VC adapter declares `scripts/task_api.py` private and only executes its pinned script; its fixed upstream credential is never bundled into this public repository.

Selection chooses a candidate, not proof of correctness. Read trigger/negative examples, confirm task phase and environment, inspect required capabilities, then execute. A selected Skill or model intent cannot approve a write. Local Git fetch/branch and stage/commit can be covered by actual explicit user task wording; remote push/MR creation, arbitrary registered processes and remote writes only allow once-scoped approval. Read-only config and explicit task prohibitions remain stronger. Permission requests include exact argv, target, content/state hashes and platform parameters. No Skill may merge MRs or deploy production through these tools.

## Setup, validation and recovery

`scripts/setup_development.py` copies three public Kaki Skills, optionally adapts the locally supplied private vc-casegen-task, writes pinned profiles and backs up config mode0600. It does not log in, change repositories, send messages or submit a test. Restart existing App/TUI processes to load new configuration.

Referenced Markdown from sibling private Skill packages is copied into this Skill's `references/upstream/` with rewritten relative links, bounded to 100 Markdown files / 8 MiB. This preserves the technical-material input contract without adding or activating the sibling Skill. Cycles are deduplicated; public source never receives those private reference files. `test_skill_install.py` verifies the installed package is self-contained.

Run `uv run pytest tests/test_development.py` for real temporary Git fetch/branch/edit/stage/commit, protected branches, foreign index preservation, context invalidation, profile integrity and input binding, once approvals, deferred discovery/private Skill reads, MR unknown receipts, timeout and cancellation. Full CI runs core/provider/context/TUI/App and frontend state tests. Live verification uses an isolated fixture before resuming an explicitly authorized business task. A remote test creation receipt or local validation success is never reported as a passed integration test.
