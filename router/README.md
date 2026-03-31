# Codeswarm Router

The Codeswarm Router is the persistent control-plane daemon responsible for:

- Managing multiple concurrent swarms
- Provisioning local, Slurm, and AWS-backed jobs
- Routing prompt injections
- Running orchestrated project scheduling
- Streaming distributed agent events
- Reconciling state with providers
- Emitting structured protocol events over TCP

It is the authoritative control boundary between the CLI/web stack and provider-backed worker execution.

---

## Architectural Role

```
CLI/Web → TCP → Router → Provider → Workers
```

The router:

- Accepts JSON commands over local TCP
- Executes provisioning locally
- Uses SSH only for cluster interactions
- Streams worker events via a single scalable SSH follower
- Maintains durable swarm registry
- Maintains durable project/task registry
- Emits versioned protocol events

---

## Control Plane

### Transport

- TCP server bound to `127.0.0.1:8765`
- JSON-line framing
- Versioned envelope: `codeswarm.router.v1`

### Envelope Format

All router events:

```json
{
  "protocol": "codeswarm.router.v1",
  "type": "event",
  "timestamp": "...",
  "event": "swarm_launched",
  "data": { ... }
}
```

Commands must include:

```json
{
  "protocol": "codeswarm.router.v1",
  "command": "...",
  "request_id": "...",
  "payload": { ... }
}
```

Invalid protocol versions are ignored.

---

## Supported Commands

Provider presets for launch can be configured via `launch_providers` in config.
Each preset selects a backend (`slurm`, `local`, or `aws`) and can include defaults and
UI field definitions for provider-specific launch parameters.
Optional `cluster_profile` (alias `cluster_config`) selects `cluster.<backend>.profiles.<name>`.
Optional `launch_soft_timeout_seconds` and `launch_hard_timeout_seconds` define per-provider
launch timeout behavior.

For local providers, launch parameters can also select the worker runtime:

- `worker_mode=codex`
- `worker_mode=claude`
- `worker_mode=mock`

Local configs may define `claude_env_profiles` so Claude launches can inject named Anthropic environment bundles such as gateway routing settings.

### `swarm_launch`

Provision a new backend job and create swarm entry.

Payload:
- `nodes`
- `system_prompt`
- `agents_md_content` (optional, copied to each worker workspace root as `AGENTS.md`)
- `agents_bundle` (optional):
  - `mode`: `file` or `directory`
  - `agents_md_content`: string copied to `AGENTS.md`
  - `skills_files`: list of `{ path, content }`
  - when `mode=directory`, skills are copied to `.agents/skills/<path>` in each worker workspace root
- `provider` (optional provider preset id)
- `provider_params` (optional provider-specific launch values)

Behavior:
1. Selects launch provider backend.
2. Passes merged `defaults + provider_params` to provider launch.
3. Prepends repo-root `AGENTS.md` to any provided `agents_md_content` / `agents_bundle.agents_md_content`.
4. If no AGENTS content is provided, repo-root `AGENTS.md` is used as default.
5. Extracts `job_id`.
6. Registers new `swarm_id`.
7. Emits `swarm_launched`.
8. Injects `system_prompt` into all nodes asynchronously when the prompt is non-empty.

### Orchestrated Projects

Router now supports an opt-in orchestrated project runtime.

Implemented capabilities include:

- project creation from explicit task lists
- planner-driven project planning
- deterministic task dispatch to idle worker nodes
- structured `TASK_RESULT` parsing
- automatic final integration task insertion
- project resume and resume preview

Project planning produces implementation tasks only. Codeswarm appends a final system-generated integration task automatically. That task:

- waits for all implementation tasks to complete
- creates `codeswarm/<project-id>/integration`
- merges the task branches in dependency order
- runs repo-level verification when available

A project is not considered fully complete until that integration task succeeds.

---

### `providers_list`

Returns launch provider catalog (id/label/backend/cluster_profile/defaults/launch_fields/launch timeout overrides) so UI can
render provider picker and provider-specific parameter forms.

---

### `inject`

Inject user content into one or more nodes.

Payload:
- `swarm_id`
- `nodes` ("all" | index | list)
- `content`

Lifecycle:
1. Generate `injection_id`
2. Emit `inject_ack`
3. Append JSON payload to remote inbox via SSH
4. Emit `inject_delivered` or `inject_failed`

---

### `swarm_list`

Returns the currently known active swarm registry after provider reconciliation.

---

### `swarm_status`

Queries the underlying provider for current liveness/state when supported.

---

### Project commands

Supported project commands now include:

- `project_create`
- `project_plan`
- `project_start`
- `project_resume`
- `project_resume_preview`
- `project_list`

---

### `swarm_terminate`

Marks swarm as `terminating`, waits for agents to become idle (best effort,
bounded by timeout), then cancels Slurm job via `scancel`.

Payload supports optional `terminate_params`:

- `download_workspaces_on_shutdown: true`

When enabled, router asks provider to export workspace/mailbox artifacts as a
tar.gz archive before backend termination and emits:

- `workspace_archive_ready` (archive created)
- `workspace_archive_failed` (archive export failed or empty)

---

## Multi-Swarm Registry

In-memory structures:

```
SWARMS: { swarm_id → { job_id, node_count, status, ... } }
JOB_TO_SWARM: { job_id → swarm_id }
LAST_USAGE: { job_id:node_id:injection_id → total_tokens }
INTER_SWARM_QUEUE: { target_swarm_id → deque[queue_item] }
```

Persistent state stored in:

```
router_state.json
```

Loaded at startup and reconciled with Slurm.
Persisted fields include swarm registry and inter-swarm queue, so queued
`enqueue_inject` work resumes after router restart.

Project state and pending planner work are also persisted in `router_state.json`.

---

## Provider Reconciliation

At startup:

For each provider, router asks for active jobs and reconciles persisted swarm state against that provider view.

For local workers on non-Linux hosts, recovery now requires fresh per-worker heartbeats rather than weak PID-only evidence. This prevents dead local swarms from being resurfaced as running after restart.

---

## Remote Event Streaming

Router launches a single remote follower:

```
ssh <login> python3 agent/outbox_follower.py <outbox_dir>
```

Follower emits JSON lines for all node outboxes.

Router:

- Uses `os.read()` for unbuffered streaming
- Parses each JSON line
- Translates worker events
- Emits structured router events over TCP

This avoids:

- Per-node `tail -F`
- Process explosion
- SSH session scaling issues

---

## Worker Event Translation

Worker runtimes currently include:

- `codex`
- `claude`
- `mock`

Router consumes runtime-specific worker output and emits a normalized event stream for the backend/UI.

For Codex, the worker emits `codex_rpc` events and router translates:

| Worker Method | Router Event |
|---------------|-------------|
| `turn/started` | `turn_started` |
| `turn/completed` | `turn_complete` |
| `agent_message_content_delta` | `assistant_delta` |
| `agent_message` | `assistant` |
| token usage updates | `usage` |

For Claude, the local Claude worker emits normalized Codeswarm events directly for:

- turn lifecycle
- assistant streaming/final text
- approval requests
- command/file edit visibility
- usage updates
- task completion

All events include:
- `swarm_id`
- `job_id`
- `node_id`
- `injection_id`

`usage` additionally includes a normalized token breakdown:
- `total_tokens`
- `input_tokens`
- `cached_input_tokens`
- `output_tokens`
- `reasoning_output_tokens`
- `last_total_tokens`
- `last_input_tokens`
- `last_cached_input_tokens`
- `last_output_tokens`
- `last_reasoning_output_tokens`
- `model_context_window`
- `usage_source` (`codex/event/token_count` or `thread/tokenUsage/updated`)

When pricing can be resolved, router also annotates usage with:

- `model_name`
- `pricing_model`
- `estimated_cost_usd`
- `last_estimated_cost_usd`

Pricing lookup is driven by:

- built-in defaults in [router.py](/Users/keithlowery/codeswarm/router/router.py)
- top-level `model_pricing` entries in the active config, which override those defaults

Claude authentication/model selection is resolved at worker launch:

- `claude_env_profile` selects a named env bundle from the active local backend config's `claude_env_profiles`
- profile values may reference `${ENV_VAR}` placeholders from the router host environment
- if no profile is selected, the worker inherits `ANTHROPIC_*` variables from the router process environment
- `claude_model` overrides the model passed to the Claude SDK
- `pricing_model` overrides which catalog entry router uses for billing

---

## Concurrency Model

Router loop is non-blocking.

- TCP server runs in background thread.
- Follower runs asynchronously.
- Slurm calls run in worker threads.
- Injection runs in background threads.

The main loop:
- Processes follower stdout
- Processes queued TCP commands
- Never blocks on SSH calls

---

## Failure Handling

### SSH Failures

- Injection emits `inject_failed`.
- Status calls time out after 15 seconds.
- Follower failure does not crash router.

### Stale TCP Clients

- Clients registered in `TCP_CLIENTS`.
- Dead connections removed automatically.
- No stdout-based fallback.

---

## Router Lifecycle

Start manually:

```bash
python router/router.py --config configs/<cluster>.json --daemon
```

The CLI normally auto-spawns it.

The router is persistent across CLI invocations.

---

## Design Principles

- Router is control-plane authority.
- Slurm provisioning must occur locally.
- All external cluster interaction via SSH.
- Single scalable follower.
- Strict protocol boundary.
- No stdio IPC.
- No implicit Slurm defaults.
- No blocking control loop.

---

## Extension Points

- WebSocket adapter for browser UI
- OpenClaw integration layer
- Swarm metrics endpoint
- Structured streaming logs
- Multi-cluster support
