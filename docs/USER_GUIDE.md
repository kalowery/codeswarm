# Codeswarm User Guide

This guide describes day-to-day usage of the current router + backend + frontend stack.

## 1. Start the system

### Option A: One command (recommended)

```bash
codeswarm web --config configs/local.json
```

or for Slurm:

```bash
codeswarm web --config configs/hpcfund.json
```

### Option B: Run services manually

Router:

```bash
python3 -u -m router.router --config configs/local.json --daemon
```

Backend:

```bash
npm --prefix web/backend run dev
```

Frontend:

```bash
npm --prefix web/frontend run dev
```

### Option C: Launch directly from the terminal

If you only need to create swarms and interact through the CLI, skip the backend/frontend:

```bash
codeswarm providers --config configs/local.json
codeswarm launch --nodes 2 --prompt "You are a focused autonomous agent." --provider local --config configs/local.json
codeswarm stop-all --config configs/local.json
```

CLI launch also supports the same launch-only payloads the web UI sends:

- `--agents <path>` for a single `AGENTS.md` file or persona directory
- repeated `--provider-param key=value`
- `--provider-params-json '{"key":"value"}'`
- `--detach` to exit immediately after launch; otherwise the CLI follows INFO activity logs by default

## 2. Modes

### Local mode

- backend: `cluster.backend = "local"`
- workers run as local subprocesses
- mailbox under `runs/mailbox` by default
- active swarms survive router restart only when the provider can prove workers are still alive
- on non-Linux hosts, local recovery uses fresh per-worker heartbeats instead of weak PID-only evidence
- stale local swarms should not reappear after router restart once their workers stop emitting heartbeats

### Slurm mode

- backend: `cluster.backend = "slurm"`
- requires `cluster.slurm.login_host` (or profile-specific `cluster.slurm.profiles.<name>.login_host`) and `cluster.slurm.*`
- mailbox under `<workspace_root>/<cluster_subdir>/mailbox`
- transient SSH writes (inject/control/state checks) use retry/backoff:
  - `cluster.slurm.ssh_retry_attempts` (default `4`)
  - `cluster.slurm.ssh_retry_delay_seconds` (default `1.5`)

### AWS mode

- backend: `cluster.backend = "aws"` (or AWS launch provider preset)
- allocates EC2 compute nodes + shared EBS volume
- mounts shared workspace at `cluster.workspace_root`
- supports multiple workers per node via provider launch fields
- optional `delete_ebs_on_shutdown` controls EBS deletion during terminate
- transient SSH writes (inject/control/bootstrap) use retry/backoff:
  - `cluster.aws.ssh_retry_attempts` (default `4`)
  - `cluster.aws.ssh_retry_delay_seconds` (default `1.5`)
- detailed setup guide: `docs/AWS_SETUP.md`

### Multi-provider launch presets

- optional config key: `launch_providers`
- each entry declares a launch preset: `id`, `label`, `backend` (`local`/`slurm`/`aws`)
- optional `cluster_profile` (or legacy `cluster_config`) binds a preset to a named backend profile
- optional `defaults` and `launch_fields` let providers expose custom launch UI inputs
- optional `launch_soft_timeout_seconds` and `launch_hard_timeout_seconds` control launch timeout behavior per preset
- backend profiles live under `cluster.<backend>.profiles.<name>`
  - useful for multiple AWS variants (cpu/gpu) and multiple Slurm login nodes/partitions

Common local presets used for deterministic project execution:

- `local-orchestrated-planner`
- `local-orchestrated-worker`

These presets default to:

- `worker_mode=codex`
- `approval_policy=never`
- `sandbox_mode=danger-full-access`
- `native_auto_approve=true`
- `fresh_thread_per_injection=true`
- `claude_env_profile=amd-llm-gateway` in the sample local configs

## 3. Launch a swarm

In UI:

- provide alias
- set node count
- set system prompt
- choose provider
- fill provider-specific launch fields (if presented)
- for Claude launches, optionally select a `Claude Env Profile`
- optionally select an Agent Persona input:
  - single file: copied as `AGENTS.md`
  - persona directory: must contain root `AGENTS.md`; `skills/` is optional
- baseline `AGENTS.md` from repo root is prepended automatically
- launch

Router emits `swarm_launched`, then injects the system prompt to all nodes when it is non-empty.

### Claude-specific notes

- `worker_mode=claude` uses the Anthropic Claude Code SDK/CLI path
- if `claude_env_profile` is unset, Claude falls back to inherited environment variables such as `ANTHROPIC_API_KEY`
- if `claude_env_profile` is set, Codeswarm expands `${ENV_VAR}` placeholders against the launch host environment and injects the resolved Anthropic env into the worker
- `approval_policy=never` maps to Claude bypass mode
- other approval policies use the normal Codeswarm approval UI for Claude tool permissions

### Agent Persona copy rules

When a persona directory is selected, each worker workspace receives:

- `AGENTS.md` in workspace root
- `.agents/skills/...` (only files under persona `skills/`)
- the final `AGENTS.md` content always starts with repo root `AGENTS.md`, then selected persona/file content

Files outside persona `AGENTS.md` and `skills/` are ignored.

## 4. Orchestrated projects

Codeswarm supports an opt-in project mode that keeps router-owned task state alongside the normal ad hoc swarm model.

Project creation paths:

- direct project creation from an explicit task list
- planner-driven project creation from a spec plus planner swarm

Repository inputs:

- absolute path to an existing local git repo
- GitHub owner/repo reference
- GitHub repo creation flow for project planning/execution

Planner mode requires:

- one planner swarm
- one or more worker swarms

Resume requires:

- one or more worker swarms
- no planner swarm is needed unless you want to replan the project itself

The router appends a final integration task automatically. A project is not complete until that integration task succeeds.

## 5. Project UI flow

In the web UI:

- create a planner swarm and worker swarms first
- open `Create Project`
- choose direct-task mode or planner mode
- for planner mode, select the planner swarm and one or more worker swarms
- submit the project

The UI lets you observe:

- project status and task counts
- the selected task prompt and acceptance criteria
- assigned swarm/node for each running task
- live worker transcript for the active swarm

## 6. Resume a project

Resume is available from the project detail pane for non-completed projects.

Resume behavior:

- opens a resume modal for the selected project
- shows a live resume preview before submission
- lets you replace the worker swarm set
- optionally retries failed tasks
- optionally reverifies completed tasks against durable task branches
- shows blocked reasons when live assignments must be terminated first

CLI equivalents:

```bash
codeswarm project resume-preview <project-id> --config configs/local.json
codeswarm project resume <project-id> --config configs/local.json
```

## 7. Inject prompts

UI supports:

- active node injection (default)
- all nodes: `/all your prompt`
- specific node: `/node[3] your prompt`
- multiple nodes/ranges: `/node[0,2-4] your prompt`
- cross-swarm idle queue: `/swarm[target-alias]/idle your prompt`
- cross-swarm idle queue with return routing: `/swarm[target-alias]/idle/reply your prompt`
- cross-swarm first idle alias: `/swarm[target-alias]/first-idle your prompt`
- cross-swarm all nodes: `/swarm[target-alias]/all your prompt`
- cross-swarm specific nodes: `/swarm[target-alias]/node[0,2-4] your prompt`

Backend maps aliases -> swarm IDs and sends either:

- `inject` (immediate delivery), or
- `enqueue_inject` (queued first-idle delivery for cross-swarm idle mode).

For routed prompts, target node turns display the injected prompt text in the turn bubble once `turn_started` arrives.

## 8. Inter-swarm queue visibility

Frontend sidebar shows queued cross-swarm work:

- source swarm -> target swarm
- selector (`idle`)
- queue age
- queued prompt content

Router events `queue_list` and `queue_updated` keep this panel synchronized.

## 9. Approval flow for tool execution

When Codex requests command approval, UI receives `exec_approval_required` and shows approval controls.

Available actions depend on `available_decisions` from worker/runtime. UI can send:

- allow / deny
- allow with policy amendment when proposed amendments are present

Backend forwards to router `/approval`, and router sends normalized control message to worker inbox.

## 10. Runtime events visible in UI

- turn lifecycle: `turn_started`, `turn_complete`
- streaming text: `assistant_delta`, `assistant`
- reasoning: `reasoning_delta`, `reasoning`
- inter-swarm queue/routing: `queue_updated`, `inter_swarm_enqueued`, `inter_swarm_dispatched`, `inter_swarm_blocked`, `inter_swarm_dropped`
- auto-routing outcomes: `auto_route_submitted`, `auto_route_ignored`
- reply-routing outcomes: `auto_reply_submitted`, `auto_reply_ignored`
- command execution: `command_started`, `command_completed`
- approvals: `exec_approval_required`, `exec_approval_resolved`
- token usage: `usage`
- errors: `agent_error`, `command_rejected`
- projects: `project_created`, `project_started`, `projects_updated`, `project_resume_preview`, `project_resumed`

## 11. Auto-routing from task completion

When a node finishes a task, backend inspects final assistant output (`task_complete`) for line-level directives:

- `/swarm[alias]/idle ...`
- `/swarm[alias]/idle/reply ...`
- `/swarm[alias]/first-idle ...`
- `/swarm[alias]/all ...`
- `/swarm[alias]/node[...] ...`

Matching lines are auto-submitted as new routes, enabling chained multi-swarm execution.
When `/reply` is used, backend correlates destination completion and injects the result back to the original sender node as a follow-up prompt.

## 12. Terminate a swarm

Use the Terminate action in UI.

Optional:

- enable `Download workspace archive on terminate` in the swarm detail pane to export workspace data before teardown.
- browser downloads a `tar.gz` archive generated on the backend host.

Router sends `swarm_terminate`, marks swarm status as `terminating`, waits for
agents to go idle (or timeout), then terminates backend resources and emits
`swarm_terminated`.

For AWS, router supports shorter graceful wait before force terminate via:

- `router.aws_graceful_terminate_timeout_seconds` (default `45`)

## 13. Attention and navigation

- node-level and swarm-level attention indicators pulse when unseen activity completes
- node tabs are horizontally scrollable for large node counts

## 14. Status and persistence notes

- Router persists swarm registry in `router_state.json`.
- Router persists inter-swarm queue state in `router_state.json`.
- Backend persists UI-facing swarm metadata in `web/backend/state.json`.
- Router persists orchestrated projects and pending project plans in `router_state.json`.
- User-terminated swarms are removed from router active state immediately after `swarm_terminated`.
- UI shows per-agent and per-swarm estimated spend from cumulative token usage.
- Optional frontend pricing env vars (USD per 1M tokens):
  - `NEXT_PUBLIC_INPUT_TOKENS_USD_PER_1M`
  - `NEXT_PUBLIC_CACHED_INPUT_TOKENS_USD_PER_1M`
  - `NEXT_PUBLIC_OUTPUT_TOKENS_USD_PER_1M`
  - `NEXT_PUBLIC_REASONING_OUTPUT_TOKENS_USD_PER_1M`
- Current defaults are set for `gpt-5.3-codex`: input `1.75`, cached input `0.175`, output `14`, reasoning output `0` (reasoning billed via output unless overridden).

## 15. Automated testing

Headless UI and project automation now exist in-repo:

```bash
npm run test:web-ui
python3 tools/orchestrated_project_resume_smoke.py
```

The browser suite uses Puppeteer and covers critical web flows including project creation, worker interaction, and resume modal behavior.

## 16. Troubleshooting

### Codex authentication

```bash
codex login
```

### Codex write failures / repeated approval prompts

Set Codex to workspace-write and disable internal approval prompts so Codeswarm approval flow remains authoritative.

### Router connectivity

Ensure router is running on `127.0.0.1:8765` and backend can connect.

Follower streams are auto-restarted by router when they drop (for both Slurm and AWS providers).

### Frontend build sanity check

```bash
npm --workspace=web/frontend run build
```

### Missing Node runtime modules (e.g. `commander`)

If `codeswarm` CLI reports missing packages after git operations, restore workspace deps:

```bash
npm install --workspaces
```
