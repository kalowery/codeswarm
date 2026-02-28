# Codeswarm

Codeswarm is an open-source distributed execution fabric for orchestrating AI agents across multiple nodes.

It is built to explore what becomes possible when large language models are treated as execution actors — not chat interfaces.

Codeswarm provides:

- A versioned control-plane protocol (`codeswarm.router.v1`)
- Multi-swarm orchestration
- Deterministic injection lifecycle management
- Parallel multi-node execution
- Normalized execution semantics
- A hardened web control plane

This project is experimental and research-oriented.

---

# Motivation

Most LLM systems are built around chat metaphors.

Codeswarm takes a different stance:

> Agents are execution units. Swarms are distributed compute actors.

We care about:

- Lifecycle authority
- Parallel execution
- Deterministic state reconciliation
- Explicit failure handling
- Cluster-backed orchestration

Execution > conversation.

---

# System Architecture

```
Slurm Worker Nodes (codex_worker.py)
        ↓  JSON-RPC (raw)
   Router (Python daemon)
        ↓  codeswarm.router.v1 (normalized events)
   Backend (Node + WebSocket bridge)
        ↓
   Frontend (Next.js control plane)
```

## Router (Authoritative Control Plane)

The router is the single source of truth.

Responsibilities:

- Swarm launch / termination
- Slurm job submission
- swarm_id ↔ job_id binding
- Event normalization
- TTL-based terminated swarm retention
- Background cleanup

The router is authoritative. Other layers mirror.

---

## Backend (Mirror + Bridge)

The backend:

- Connects to router via TCP
- Broadcasts events via WebSocket
- Performs symmetric reconciliation on `swarm_list`
- Forwards structured failures (`command_rejected`)

It never invents lifecycle state.

---

## Frontend (Execution-Aware UI)

The web UI renders:

- Per-node execution streams
- Reasoning traces
- Tool execution blocks
- Token usage
- Lifecycle transitions

Optimistic UI is reconciled against router authority.

---

# Core Concepts

## Swarm

A distributed execution unit spanning one or more nodes.

Each swarm has:

- `swarm_id`
- `job_id`
- `node_count`
- `status`

---

## Node

An isolated execution context within a swarm.

Workspace layout:

```
runs/<job_id>/node_XX/
```

---

## Injection

A prompt stimulus delivered to one or more nodes.

Each injection results in a bounded execution **Turn**.

---

## Turn

A deterministic execution cycle that may include:

- `turn_started`
- `reasoning_delta`
- `assistant_delta`
- `command_started`
- `command_completed`
- `usage`
- `turn_complete`

---

# Quick Start

## Run Full Web Stack

```bash
codeswarm web --config configs/hpcfund.json
```

This launches:

- Router (daemon)
- Backend (WebSocket bridge)
- Frontend (Next.js dev server)

---

## CLI Usage

Launch a swarm:

```bash
codeswarm launch \
  --nodes 4 \
  --prompt "You are a focused autonomous agent." \
  --config configs/hpcfund.json
```

Inject into a swarm:

```bash
codeswarm inject <swarm_id> \
  --prompt "Optimize GEMM tiling." \
  --config configs/hpcfund.json
```

List swarms:

```bash
codeswarm list --config configs/hpcfund.json
```

Terminate:

```bash
codeswarm terminate <swarm_id> --config configs/hpcfund.json
```

---

# Multi-Node Execution

Codeswarm supports multi-node swarms (subject to cluster policy).

SBATCH configuration:

```
#SBATCH --nodes=N
#SBATCH --ntasks=N
```

Each node runs an isolated worker instance.

Parallel injection is supported.

---

# Failure Model

Codeswarm treats distributed signals carefully:

- SSH timeout ≠ termination
- Single Slurm `NOT_FOUND` ≠ authoritative deletion
- Failures propagate via `command_rejected`
- Frontend reconciles optimistic state

Terminated swarms are retained via TTL (default 15 minutes) to prevent lifecycle flapping.

---

# Cluster Provider Abstraction

Execution backends are pluggable via a `ClusterProvider` interface.

Current production provider: **SlurmProvider**.

Future providers may include:

- Kubernetes
- AWS
- Local development runtime

Protocol remains stable regardless of provider.

---

# Documentation

- White paper: `WHITEPAPER.md`
- Architecture reference: `docs/architecture.md`
- CLI docs: `cli/README.md`
- Protocol spec: `docs/PROTOCOL.md`

---

# Status

Control-plane stable.
Multi-swarm orchestration supported.
Multi-node execution supported (policy-dependent).
Web UI hardened for lifecycle reconciliation.

Experimental by design.

---

# License

MIT License.

See `LICENSE` for details.
