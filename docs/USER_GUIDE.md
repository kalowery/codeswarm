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

## 3. Launch a swarm

In UI:

- provide alias
- set node count
- set system prompt
- launch

Router emits `swarm_launched`, then injects the system prompt to all nodes.

## 4. Inject prompts

UI supports:

- active node injection (default)
- all nodes: `/all your prompt`
- specific node: `/node[3] your prompt`
- multiple nodes/ranges: `/node[0,2-4] your prompt`

Backend maps alias -> swarm_id and sends `inject` command to router.

## 5. Approval flow for tool execution

When Codex requests command approval, UI receives `exec_approval_required` and shows approval controls.

Available actions depend on `available_decisions` from worker/runtime. UI can send:

- allow / deny
- allow with policy amendment when proposed amendments are present

Backend forwards to router `/approval`, and router sends normalized control message to worker inbox.

## 6. Runtime events visible in UI

- turn lifecycle: `turn_started`, `turn_complete`
- streaming text: `assistant_delta`, `assistant`
- reasoning: `reasoning_delta`, `reasoning`
- command execution: `command_started`, `command_completed`
- approvals: `exec_approval_required`, `exec_approval_resolved`
- token usage: `usage`
- errors: `agent_error`, `command_rejected`

## 7. Terminate a swarm

Use the Terminate action in UI.

Router sends `swarm_terminate`, provider terminates backend resources, and UI receives `swarm_removed`.

## 8. Attention and navigation

- node-level and swarm-level attention indicators pulse when unseen activity completes
- node tabs are horizontally scrollable for large node counts

## 9. Status and persistence notes

- Router persists swarm registry in `router_state.json`.
- Backend persists UI-facing swarm metadata in `web/backend/state.json`.
- Router marks terminated swarms, then prunes them by TTL/cap (`swarm_removed`).

## 10. Troubleshooting

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
