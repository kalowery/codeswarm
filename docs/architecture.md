# Codeswarm Architecture

This document describes the internal architecture, lifecycle model, and authority boundaries of Codeswarm.

Codeswarm is a distributed execution fabric for orchestrating AI agents across multiple nodes. It is not a chat wrapper; it is a lifecycle‑aware control plane.

---

# 1. High-Level Topology

```
Slurm Worker Nodes (codex_worker.py)
        ↓  JSON-RPC (raw)
   Router (Python)
        ↓  codeswarm.router.v1 (normalized events)
   Backend (Node + WebSocket)
        ↓  WebSocket broadcast
   Frontend (Next.js / Zustand store)
```

Each layer has clearly defined responsibilities.

---

# 2. Authority Model

Authority is singular and layered.

## Router — Source of Truth

The router is the authoritative lifecycle controller.

Responsibilities:

- Swarm creation and termination
- Slurm job submission
- swarm_id ↔ job_id binding
- Worker connection management
- Event normalization
- TTL-based terminated swarm retention

If the router says a swarm exists, it exists.
If the router says it is removed, it is removed.

No other layer invents swarm lifecycle state.

---

## Backend — Mirror + Bridge

The backend is not authoritative.

Responsibilities:

- TCP client to router
- WebSocket bridge to frontend
- Request tracking (request_id ↔ swarm_id)
- Authoritative reconciliation on `swarm_list`
- Removal of stale swarms

On `swarm_list`, backend performs **symmetric reconciliation**:

- Remove local swarms not present in router
- Add new swarms from router

Backend never merges blindly.

---

## Frontend — Event-Sourced View

Frontend renders execution state derived from router events.

Responsibilities:

- Event-driven state updates
- Optimistic injection UI
- Reconciliation on `command_rejected`
- Per-node execution visualization

Frontend does not invent lifecycle state.

---

# 3. Core Domain Concepts

## Swarm

A distributed execution unit spanning one or more nodes.

Properties:

- swarm_id (UUID)
- job_id (Slurm)
- node_count
- status
- slurm_state

Swarm state is persisted by router.

---

## Node

An isolated worker execution context.

Each node has:

- node_id
- independent turns
- isolated workspace

Workspace layout:

```
runs/<job_id>/node_XX/
```

---

## Injection

A prompt stimulus delivered to one or more nodes.

Each injection results in a bounded **Turn**.

---

## Turn

A deterministic execution cycle.

Events within a turn may include:

- turn_started
- reasoning_delta
- assistant_delta
- command_started
- command_completed
- usage
- turn_complete

Turns are isolated per node.

---

# 4. Event Normalization

Workers emit raw JSON-RPC events.

Router translates these into a stable protocol:

```
codeswarm.router.v1
```

Only execution-semantic events are normalized.

Raw payload is preserved under:

```
"raw": <original_payload>
```

This prevents tight coupling to worker internals.

---

# 5. Lifecycle Flow

## Swarm Launch

1. Backend sends `swarm_launch`
2. Router submits SBATCH
3. Slurm returns job_id
4. Router binds swarm_id ↔ job_id
5. Router emits `swarm_launched`
6. Backend mirrors
7. Frontend reconciles

If SBATCH fails:

- Router emits `command_rejected`
- Backend forwards
- Frontend removes optimistic ghost

---

## Injection Flow

1. Frontend sends injection request
2. Backend forwards to router
3. Router delivers to worker(s)
4. Worker emits raw events
5. Router normalizes
6. Backend broadcasts
7. Frontend updates per-node turn state

---

## Termination Flow

1. Terminate command issued
2. Router marks swarm terminated
3. `terminated_at` timestamp recorded
4. Swarm retained for TTL (default 900s)
5. Background cleanup prunes expired swarms
6. Router emits `swarm_removed`
7. Backend mirrors removal
8. Frontend drops swarm

---

# 6. TTL-Based Retention

Immediate deletion caused lifecycle flapping under Slurm inconsistency.

Current model:

- TERMINATED_TTL_SECONDS = 900
- MAX_TERMINATED = 100
- Background cleanup loop (60s)

This prevents premature deletion under transient failures.

---

# 7. Failure Semantics

## SSH Timeouts

SSH probe failure is not authoritative termination.

## Slurm NOT_FOUND

Single NOT_FOUND is not sufficient to delete swarm.

## command_rejected

Used for:

- Launch failure
- Inject failure
- Invalid swarm_id

Frontend must reconcile optimistic state.

---

# 8. Multi-Node Execution

SBATCH configuration:

```
#SBATCH --nodes=N
#SBATCH --ntasks=N
```

Worker isolation per SLURM_PROCID.

Parallel injection supported.

Each node maintains independent turn history.

---

# 9. Design Principles

1. Single lifecycle authority (router)
2. Symmetric reconciliation
3. Execution > conversation
4. Preserve raw payload for forward compatibility
5. Treat cluster signals as eventually consistent
6. Prefer explicit removal over implicit disappearance

---

# 10. Current Limitations

- No worker heartbeat model yet
- Slurm polling still external signal
- Injection targeting UI evolving
- Backend state not event-sourced persistently

---

# 11. Future Architecture Directions

- Worker heartbeat for liveness
- Persistent swarm registry
- Event-sourced backend state
- Multi-cluster router federation
- Agent-to-agent coordination layer

---

Codeswarm is intentionally opinionated.

It favors explicit lifecycle semantics over convenience.

It treats distributed execution as a systems problem, not a UI feature.
