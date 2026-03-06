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

## 2. Modes

### Local mode

- backend: `cluster.backend = "local"`
- workers run as local subprocesses
- mailbox under `runs/mailbox` by default

### Slurm mode

- backend: `cluster.backend = "slurm"`
- requires `ssh.login_alias` and `cluster.slurm.*`
- mailbox under `<workspace_root>/<cluster_subdir>/mailbox`

### AWS mode

- backend: `cluster.backend = "aws"` (or AWS launch provider preset)
- allocates EC2 compute nodes + shared EBS volume
- mounts shared workspace at `cluster.workspace_root`
- supports multiple workers per node via provider launch fields
- optional `delete_ebs_on_shutdown` controls EBS deletion during terminate
- detailed setup guide: `docs/AWS_SETUP.md`

### Multi-provider launch presets

- optional config key: `launch_providers`
- each entry declares a launch preset: `id`, `label`, `backend` (`local`/`slurm`/`aws`)
- optional `defaults` and `launch_fields` let providers expose custom launch UI inputs

## 3. Launch a swarm

In UI:

- provide alias
- set node count
- set system prompt
- choose provider
- fill provider-specific launch fields (if presented)
- launch

Router emits `swarm_launched`, then injects the system prompt to all nodes.

## 4. Inject prompts

UI supports:

- active node injection (default)
- all nodes: `/all your prompt`
- specific node: `/node[3] your prompt`
- multiple nodes/ranges: `/node[0,2-4] your prompt`
- cross-swarm idle queue: `/swarm[target-alias]/idle your prompt`
- cross-swarm first idle alias: `/swarm[target-alias]/first-idle your prompt`
- cross-swarm all nodes: `/swarm[target-alias]/all your prompt`
- cross-swarm specific nodes: `/swarm[target-alias]/node[0,2-4] your prompt`

Backend maps aliases -> swarm IDs and sends either:

- `inject` (immediate delivery), or
- `enqueue_inject` (queued first-idle delivery for cross-swarm idle mode).

For routed prompts, target node turns display the injected prompt text in the turn bubble once `turn_started` arrives.

## 5. Inter-swarm queue visibility

Frontend sidebar shows queued cross-swarm work:

- source swarm -> target swarm
- selector (`idle`)
- queue age
- queued prompt content

Router events `queue_list` and `queue_updated` keep this panel synchronized.

## 6. Approval flow for tool execution

When Codex requests command approval, UI receives `exec_approval_required` and shows approval controls.

Available actions depend on `available_decisions` from worker/runtime. UI can send:

- allow / deny
- allow with policy amendment when proposed amendments are present

Backend forwards to router `/approval`, and router sends normalized control message to worker inbox.

## 7. Runtime events visible in UI

- turn lifecycle: `turn_started`, `turn_complete`
- streaming text: `assistant_delta`, `assistant`
- reasoning: `reasoning_delta`, `reasoning`
- inter-swarm queue/routing: `queue_updated`, `inter_swarm_enqueued`, `inter_swarm_dispatched`, `inter_swarm_blocked`, `inter_swarm_dropped`
- auto-routing outcomes: `auto_route_submitted`, `auto_route_ignored`
- command execution: `command_started`, `command_completed`
- approvals: `exec_approval_required`, `exec_approval_resolved`
- token usage: `usage`
- errors: `agent_error`, `command_rejected`

## 8. Auto-routing from task completion

When a node finishes a task, backend inspects final assistant output (`task_complete`) for line-level directives:

- `/swarm[alias]/idle ...`
- `/swarm[alias]/first-idle ...`
- `/swarm[alias]/all ...`
- `/swarm[alias]/node[...] ...`

Matching lines are auto-submitted as new routes, enabling chained multi-swarm execution.

## 9. Terminate a swarm

Use the Terminate action in UI.

Router sends `swarm_terminate`, marks swarm status as `terminating`, waits for
agents to go idle (or timeout), then terminates backend resources and emits
`swarm_removed`.

## 10. Attention and navigation

- node-level and swarm-level attention indicators pulse when unseen activity completes
- node tabs are horizontally scrollable for large node counts

## 11. Status and persistence notes

- Router persists swarm registry in `router_state.json`.
- Router persists inter-swarm queue state in `router_state.json`.
- Backend persists UI-facing swarm metadata in `web/backend/state.json`.
- Router marks terminated swarms, then prunes them by TTL/cap (`swarm_removed`).
- UI shows per-agent and per-swarm estimated spend from cumulative token usage.
- Optional frontend pricing env vars (USD per 1M tokens):
  - `NEXT_PUBLIC_INPUT_TOKENS_USD_PER_1M`
  - `NEXT_PUBLIC_CACHED_INPUT_TOKENS_USD_PER_1M`
  - `NEXT_PUBLIC_OUTPUT_TOKENS_USD_PER_1M`
  - `NEXT_PUBLIC_REASONING_OUTPUT_TOKENS_USD_PER_1M`
- Current defaults are set for `gpt-5.3-codex`: input `1.75`, cached input `0.175`, output `14`, reasoning output `0` (reasoning billed via output unless overridden).

## 12. Troubleshooting

### Codex authentication

```bash
codex login
```

### Codex write failures / repeated approval prompts

Set Codex to workspace-write and disable internal approval prompts so Codeswarm approval flow remains authoritative.

### Router connectivity

Ensure router is running on `127.0.0.1:8765` and backend can connect.

### Frontend build sanity check

```bash
npm --workspace=web/frontend run build
```
