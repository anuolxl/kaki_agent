# Devbox compilation Skill

The Agent/Right Code connection, local SQLite and TUI/App remain on macOS. The `kaki-devbox-build` Skill delegates source/dependency preparation and build/test execution over a target fixed in trusted command-profile environment `KAKI_DEVBOX_TARGET=user@host`. It is not a remote Agent deployment, SSH daemon or arbitrary shell tool.

## Components and control/data flow

`Kaki task → Skill selection → kaki_command → pinned local adapter → SSH → Python3.5 stdlib worker → dedicated Git checkout/build child → status.json + build.log → SSH status → paired ToolResult → next model step`.

`devbox_probe.py` observes metadata. `workflow.py` transfers an exact-commit Git bundle with SHA256 and returns private local receipts. `remote_worker.py` creates an owner-marked private root and new validation branch, starts bounded detached builds and records terminal facts. Profiles register no free host, command, key, system install, SDK publication or deployment parameter. Repo identifiers are restricted to `sdk` and `media` and must match the active local workspace.

## Local profiles

| Name | Capability / timeout | Accepted suffix flags |
|---|---|---|
| devbox-inspect | external.read / 100s | none |
| devbox-prepare | external.write / 1500s | repo, branch, commit, task |
| devbox-dependencies | external.write / 1500s | task; nine fixed SDK/parent/BPT BUILD dependencies only |
| devbox-go-cache | external.write / 1500s | task; materializes declared Go module archives only |
| devbox-cancel | external.write / 100s | task, job; verifies owned process group |
| devbox-build | external.write / 100s; starts background job | repo, mode, task, job, sdk-commit, timeout |
| devbox-status | external.read / 100s | task, job, wait, lines |

Prefix arguments fix the action and local state directory. SSH target and agent socket use administrator config, never model-generated options. Source and worker scripts are SHA256-pinned in a content-addressed `skill-runtimes/kaki-devbox-build/<package-sha>/` directory; installing a newer Skill leaves active versions intact. Every remote write still follows normal once-scoped approval. Config installation preserves existing custom protected branches, explicit disabled push/MR settings and provider secrets, and backs up config mode0600. The selected Python venv path is kept lexically absolute rather than resolving its symlink away from its environment.

## JSON contracts

| Object | Fields / defaults | Constructor and consumers |
|---|---|---|
| Request | action required: upload/prepare/start/status; repo sdk/media; task slug≤80; commit 40 lowercase hex for prepare; branch validated locally; job slug for start/status; mode sdk-build/sdk-test/media-build/media-test; sdk_commit required for Media link; timeout=1800,30..7200; wait=0,0..30; lines=40,1..120 | Local adapter argparse→remote dispatch; unknown modes/IDs fail |
| Owner marker | schema=1,owner_uid,created | owned_root; existing foreign/unmarked directories refused |
| Source record | repo,path,branch,source_commit,bundle_sha256,prepared | prepare→source-<repo>.json; worker verifies HEAD and preserves original repos |
| Job record | schema=1,task,job,repo,mode,sdk_commit,timeout,status,created; pid/child_pid/phase/started/command/cwd/go_version/source_commit/linked_sdk/exit_code/ended/error appear during execution | exclusive job directory; worker atomic JSON replacement; status returns original/derived state |
| Linked SDK | source_commit,library,sha256,modfile | Media build plan; exact SDK commit and existing compiled lib required |
| SDK build manifest | schema=1,job,source_commit,artifact_root,libraries[{path,sha256,size}] | SDK build copies outputs into its job directory; Media checks source/artifact provenance even after SDK test clears checkout/output |
| Local receipt | target,request,result | state/workflows/devbox/<task>-<job-or-repo>.json; recovery/audit |
| Launch receipt | pid required | job/launch.json records the spawned worker independently of its queued/running/terminal status; status uses it to detect a lost queued worker |
| Dependency manifest | map of log/log_sdk/metrics2/metrics_sdk/program/consul/monitoring/env/sonic to repo,ref,qualified_ref,source_commit,bundle_sha256,path; optional declaration{path,sha256} | local Git read→bundle→remote bare mirror; dependencies.json; worker verifies fixed ref and commit before applying process-local URL rewrites |

Job phases: queued→running→succeeded/failed. Missing worker without a terminal receipt is unknown; do not replay start. Same job and same mode reuse its result; different repo/mode/SDK cannot reuse that job. Builds sharing one task root acquire a build lock. Startup creates a new session; SSH disconnect/Agent exit does not cancel it. Build child has a 30..7200s deadline and process-group termination on timeout.

## Compilation semantics

SDK uses its canonical build.sh/build.sh test with Blade initialization confined to the dedicated root. When present, the official `/opt/tiger/typhoon-blade/blade` precedes legacy project-local Blade in the build process PATH, recorded as blade_path; user shell configuration and existing tool installations are not rewritten. Blade/BPT may populate their normal shared toolchain/package caches while resolving the repository's declared dependencies. Media uses Go1.23, CGO=1, temporary go.mod/go.sum with SDK replace and GOWORK=off. The library must match both the manifest's source commit and SHA256. `go build` output and logs stay in job directories; `make test` uses the repository's prescribed environment. The original Media build.sh writes netrc/shared system library directories, so it is not run as a whole in a shared validation environment. Tracked-source changes from any build are retained and reported as failed, never silently discarded.

Successful SSH/start/status return codes do not imply build success. Only terminal job status and actual child exit code prove a command result; report source SHA, command, log and SDK library SHA separately. No tests publish SDKs, change formal go.mod, deploy services or merge MRs. Existing user worktrees/index/branch refs are not reused/reset/stashed. Git bundle transfer contains committed objects, not uncommitted edits or separately managed LFS data; missing build dependencies must be diagnosed explicitly.

## Validation and installation

If Devbox Git SSH authentication is absent, `devbox-dependencies --task <task>` reads the nine fixed SDK/parent/BPT BUILD versions through existing local Git authentication and transfers Git bundles. It does not send credentials or bypass a repository ACL. The worker uses `GIT_CONFIG_PARAMETERS` (compatible with the actual Git2.29 host) to redirect these exact URLs to task-local bare mirrors only for the build child; no global Git config changes or agent forwarding. Parent BUILD declarations are checked in dependency order even on an empty cache. Qualified refs prefer refs/heads over refs/tags, matching git clone -b when both names exist with different commits; qualified_ref is added to new receipts while old receipts remain readable. Unknown dependencies still require their real read access and fail explicitly. Git network operations use BatchMode/connect/keepalive limits instead of waiting indefinitely for interactive authentication.

`tests/test_devbox_skill.py` exercises exact bundle→isolated branch, original index/HEAD preservation, dirty/foreign directory refusal, job identity, actual local fixture child exit/log, Python3.5 syntax and target injection rejection. These fixtures do not contact a real Devbox. Real SSH/SDK/Media verification is executed by Kaki through configured profiles and recorded in its session.

`uv run python scripts/setup_development.py --harness <be-vcs> --python <Kaki venv Python> --devbox-target <user@host>` installs the self-contained Skill and seven profiles without logging in or starting a build. Restart App/TUI to read changed profiles. Keep the target out of public source; no private keys or model API keys are transferred.

BPT rapidjson 1.1.0 itself declares middleware/sonic-cpp@1.1.0. That fixed source mirror is allowed by the pinned workflow; its declaration is checked on the remote when installed, and must be present and match before an SDK build/test can report success if this mirror was prepared. JobRecord.dependency_declarations={} is optional for old receipts; entries contain the parent BUILD path/SHA256. This reads package metadata without modifying BPT caches.

## Go source cache materialization and cancellation

The actual Devbox has GOPRIVATE/GONOPROXY bypassing proxies, no usable Git SSH identity and an unauthenticated company proxy returning401. devbox-go-cache parses the committed Media/SDK go.mod requirements and main replacements, includes metadata from go.sum and the CI init-patcher v0.0.5 tool, inventories missing remote archives, and copies only declared .mod/.info/.zip files from the local module cache. Missing archives may be downloaded locally with existing local authentication; no local business compilation occurs. A SHA256-checked tar is validated for paths, file types/counts/sizes and exact source commits before extracting into a task-private file GOPROXY. Go continues checking existing go.sum. No credentials, netrc or keys are transferred; no global Go settings or formal modfiles change.

GoProxyManifest{path,bundle_sha256,media_commit,sdk_commit,module_count,file_count} is stored as task/go-proxy.json. Transfer manifest has schema=1 and files{relative_path:size}; maximum3000 required modules/12000 metadata versions/20000 files/8GiB. JobRecord.go_proxy=None is optional for old jobs. The build process sets GOPROXY to that exact directory URI and GONOPROXY=none. The official module reference documents file proxies and GONOPROXY behavior: https://go.dev/ref/mod#environment-variables . Media tests install init-patcher only into task/.tools/bin with Go1.23; the tool install clears the Media modfile GOFLAGS and uses CGO=0, while the actual Media command retains CGO=1 and temporary SDK linkage.

cancel validates task owner, worker cmdline, child parent PID and child process group before SIGTERM. Queued work records a cancellation request and is checked before building; new workers report cancelled with the actual child code, while an older worker may report failed/-15. Cancellation never changes source files or protected refs. It does not claim to roll back completed external side effects. Unknown/reused PIDs are refused.


SDK runtime libraries include glibc and must not enter the Go/Git/compiler LD_LIBRARY_PATH. Compilation uses CGO include/-L/rpath-link flags with the original host environment. Media build outputs are configured with the SDK's loader/RPATH in their private job directory; native Go tests use a quoted -exec runner invoking that loader with the SDK runtime library path only for the test binary. This fixes an observed Git VCS-stamping segfault caused by runtime-library contamination. The runner preserves argv verbatim and does not invoke a new Agent or service.
