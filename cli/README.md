# Codeswarm CLI

The Codeswarm CLI is the control-plane client for managing distributed Codex swarms running under Slurm.

It communicates with the Codeswarm router daemon over a local TCP control channel (`127.0.0.1:8765`) using a versioned JSON-line protocol (`codeswarm.router.v1`).

---

## Architecture

```
CLI (Node.js)
   ↓ TCP (127.0.0.1:8765)
Router (Python daemon)
   ↓ SSH
HPC Login Node
   ↓ Slurm
Compute Nodes
```

### Control Plane

- Transport: Local TCP (JSON lines)
- Protocol: `codeswarm.router.v1`
- Router auto-spawned by CLI (detached mode)
- CLI retries TCP connection until router binds

### Event Model

All responses are structured protocol envelopes:

```json
{
  "protocol": "codeswarm.router.v1",
  "type": "event",
  "timestamp": "...",
  "event": "swarm_launched",
  "data": { ... }
}
```

CLI matches responses by `request_id`.

---

## Installation

From the `codeswarm/cli` directory:

```bash
npm install
npm run build
```

Requirements:
- Node.js 18+
- TypeScript (installed via dev dependency)

---

## Usage

All commands require either:

```
--config <router_config.json>
```

Router config must point to your HPC cluster settings.

---

### Launch a Swarm

After linking for development:

```bash
npm link
```

Launch:

```bash
codeswarm launch \
  --nodes 4 \
  --prompt "You are a focused autonomous agent." \
  --config ../configs/hpcfund.json
```

#### Required Flags

| Flag | Description |
|------|------------|
| `--nodes` | Number of swarm nodes |
| `--prompt` | System prompt injected at launch |

Backend-specific parameters (partition, time, account, etc.) are defined in the router configuration file.

#### Output Example

```
Swarm launched successfully:

request_id: ...
swarm_id: ...
job_id: ...
node_count: 4
```

---

### List Swarms

```bash
codeswarm list --config ../configs/hpcfund.json
```

Returns all known swarms from router state.

---

### Swarm Status

```bash
codeswarm status <swarm_id> \
  --config ../configs/hpcfund.json
```

Includes:

- Router state
- Slurm state (via `squeue`)

---

### Inject Prompt

Inject into all nodes:

```bash
codeswarm inject <swarm_id> \
  --prompt "Refactor kernel memory access." \
  --config ../configs/hpcfund.json
```

Inject into specific node:

```bash
codeswarm inject <swarm_id> \
  --nodes 0 \
  --prompt "Focus on GEMM tiling strategy." \
  --config ../configs/hpcfund.json
```

---

## Router Lifecycle

The CLI:

1. Spawns router in detached mode.
2. Retries TCP connection (up to 5 seconds).
3. Sends command.
4. Waits for matching `request_id`.
5. Exits after response.

Router runs in persistent daemon mode.

Multiple CLI invocations reuse the same router instance.

---

## Transport Guarantees

- JSON-line framing with persistent TCP buffer.
- Retry-based connection startup.
- Client registration required for event emission.
- No stdio IPC.
- No UNIX sockets.

This eliminates:

- Pipe buffering issues
- TTY starvation
- Spawn race conditions
- Broken stdout parsing

---

## Operational Notes

### Slurm Requirements

Cluster must require explicit:

- `--partition`
- `--time`

Router will reject invalid launch payloads.

### Launch Duration

Swarm launch may take up to 60+ seconds depending on cluster load.

CLI waits indefinitely for response.

---

## Troubleshooting

### `ECONNREFUSED 127.0.0.1:8765`

Router not yet bound.

Fix:
- Ensure no stale routers
- Retry CLI command

### Multiple router instances

Check:

```bash
ps aux | grep router.py
```

Kill stale:

```bash
pkill -f router.py
```

### No CLI Output

Verify:
- Router has registered TCP client
- No stale router instance
- `router_state.json` not corrupted

---

## Development

Rebuild CLI:

```bash
npm run build
```

Router path resolution:

```
../../router/router.py
```

Transport implementation:

```
cli/src/router/transport/TcpTransport.ts
```

---

## Future Extensions

- Persistent CLI shell mode
- Web UI over same TCP protocol
- OpenClaw adapter layer
- Swarm termination command
- Structured streaming output mode
