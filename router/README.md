# Codeswarm Router

The Codeswarm Router is the persistent control-plane daemon responsible for:

- Managing multiple concurrent swarms
- Provisioning Slurm jobs
- Routing prompt injections
- Streaming distributed agent events
- Reconciling state with Slurm
- Emitting structured protocol events over TCP

It is the authoritative control boundary between the CLI and the HPC cluster.

---

## Architectural Role

```
CLI → TCP → Router → SSH → Slurm → Compute Nodes
```

The router:

- Accepts JSON commands over local TCP
- Executes provisioning locally
- Uses SSH only for cluster interactions
- Streams worker events via a single scalable SSH follower
- Maintains durable swarm registry
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

### `swarm_launch`

Provision a new Slurm job and create swarm entry.

Payload:
- `nodes`
- `partition`
- `time`
- `account` (optional)
- `qos` (optional)
- `system_prompt`

Behavior:
1. Calls `allocate_and_prepare.py` locally.
2. Extracts `job_id`.
3. Registers new `swarm_id`.
4. Emits `swarm_launched`.
5. Injects `system_prompt` into all nodes asynchronously.

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

Returns all known swarms from in-memory registry.

---

### `swarm_status`

Queries Slurm via:

```
ssh <login> squeue -j <job_id> -h -o '%T'
```

Executed in background thread to avoid blocking control loop.

---

### `swarm_terminate`

Cancels Slurm job via `scancel`.

---

## Multi-Swarm Registry

In-memory structures:

```
SWARMS: { swarm_id → { job_id, node_count, status, ... } }
JOB_TO_SWARM: { job_id → swarm_id }
LAST_USAGE: { injection_id → token_count }
```

Persistent state stored in:

```
router_state.json
```

Loaded at startup and reconciled with Slurm.

---

## Slurm Reconciliation

At startup:

```
squeue -h -o '%i|%j|%T'
```

For each known swarm:

- If job present → `running`
- If missing → `terminated`

This ensures router restart safety.

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

Worker emits `codex_rpc` events.

Router translates:

| Worker Method | Router Event |
|---------------|-------------|
| `turn/started` | `turn_started` |
| `turn/completed` | `turn_complete` |
| `agent_message_content_delta` | `assistant_delta` |
| `agent_message` | `assistant` |
| token usage updates | `usage` |

All events include:
- `swarm_id`
- `job_id`
- `node_id`
- `injection_id`

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
