# Codeswarm

Codeswarm is a scalable, Slurm-native distributed Codex runtime for HPC clusters.

It provides a versioned TCP control plane, multi-swarm orchestration, correlated injection lifecycle, and streaming agent responses across distributed compute nodes.

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
   ↓ Shared Filesystem Mailbox
Outbox Follower → Router → CLI
```

### Core Components

- **CLI** – User-facing control client (Node.js)
- **Router** – Persistent control-plane daemon (Python)
- **Slurm Allocator** – Provisions nodes
- **Codex Worker** – Runs on compute nodes
- **Mailbox Transport** – Shared filesystem JSONL event stream
- **Outbox Follower** – Single scalable SSH streaming process

---

## Control Plane

Transport:
- Local TCP (`127.0.0.1:8765`)
- JSON-line protocol
- Versioned envelope: `codeswarm.router.v1`

The CLI automatically spawns and connects to the router daemon.

All responses are correlated by `request_id`.

---

## Multi-Swarm Model

Codeswarm supports multiple concurrent swarms.

Each swarm has:

- `swarm_id`
- `job_id`
- `node_count`
- `system_prompt`
- `status`

All runtime events include:

- `swarm_id`
- `job_id`
- `node_id`
- `injection_id`

---

## Quick Start (CLI Recommended)

From `codeswarm/cli`:

```bash
npm install
npm run build
```

Launch a swarm:

```bash
node dist/index.js launch \
  --nodes 4 \
  --partition mi2508x \
  --time 00:10:00 \
  --prompt "You are a focused autonomous agent." \
  --config ../configs/hpcfund.json
```

Inject into swarm:

```bash
node dist/index.js inject <swarm_id> \
  --prompt "Optimize GEMM tiling." \
  --config ../configs/hpcfund.json
```

Check status:

```bash
node dist/index.js status <swarm_id> \
  --config ../configs/hpcfund.json
```

---

## Router (Advanced Usage)

Manual router startup:

```bash
python router/router.py --config configs/<cluster>.json --daemon
```

The router:

- Maintains persistent swarm registry
- Reconciles state with Slurm
- Streams worker events via single SSH follower
- Emits structured JSON events over TCP
- Never blocks control loop

---

## Slurm Provisioning Flow

```
Router (local)
   → allocate_and_prepare.py (local)
      → SSH
         → sbatch
```

Provisioning always occurs locally on router host.

Required Slurm flags:
- `--partition`
- `--time`

---

## Transport Guarantees

- No stdio IPC
- No UNIX sockets
- No pipe buffering issues
- Deterministic TCP handshake with retry
- Buffered TCP framing
- Client registration for event emission
- Single scalable SSH follower (no per-node tails)

---

## Cluster Requirements

Codeswarm assumes:

- Shared filesystem between login and compute nodes
- Passwordless SSH
- Slurm with `--signal=TERM@60` support
- squeue accessible from login node

---

## System Status (2026‑02‑25)

Stable:

- TCP control plane
- Multi-swarm orchestration
- Correlated injection lifecycle
- Non-blocking daemon
- Persistent router state
- Slurm reconciliation
- Structured event streaming

Ready for:

- OpenClaw integration
- Web UI reuse
- Production HPC workflows

---

## Documentation

- CLI docs: `cli/README.md`
- Full guide: `docs/USER_GUIDE.md`
