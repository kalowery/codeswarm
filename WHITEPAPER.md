# Codeswarm
### Toward Distributed Execution Fabrics for Multi‑Agent Systems

---

## Abstract

Most large language model systems today live inside chat boxes.

Codeswarm asks a different question:

> What happens when we stop treating LLMs as conversational partners and start treating them as distributed execution actors?

Codeswarm is an open-source experiment in orchestrating AI agents across multiple nodes using Slurm-managed infrastructure. It provides lifecycle authority, event normalization, execution semantics, and a control plane designed for parallel agent execution.

This paper describes the architecture, design philosophy, failure semantics, and lessons learned while building a distributed execution fabric for multi-agent systems.

It is not a product pitch.
It is a systems experiment.

---

## 1. The Chat Interface Is a Trap

The modern AI ecosystem is obsessed with chat.

Everything is a conversation.
Everything is a prompt box.

But real work is not a conversation.

Real work involves:

- Parallel tasks
- Tool execution
- File system mutations
- Failure recovery
- Lifecycle control
- Resource scheduling
- Termination authority

Chat interfaces collapse all of this into a single stream of tokens.

That works for demos.
It does not work for orchestration.

Codeswarm starts from a different premise:

> Agents are execution units, not chat transcripts.

---

## 2. What Codeswarm Actually Is

Codeswarm is a distributed execution fabric for launching and managing AI agents across multiple nodes.

At its core:

- A **Swarm** is a distributed unit of execution.
- A **Node** is an isolated agent worker.
- An **Injection** is a deterministic stimulus.
- A **Turn** is a bounded execution cycle.

Each node operates independently within a swarm.
Each injection can target one, some, or all nodes.

The system is built to explore parallelism — not simulate a conversation.

---

## 3. Architectural Overview

```
Slurm Worker Nodes
        ↓
   Router (Python)
        ↓
   Backend (Node/WebSocket)
        ↓
   Frontend (Next.js)
```

### Router (Authoritative Control Plane)

The router is the single source of truth.

Responsibilities:

- Swarm lifecycle management
- Slurm job submission
- Event normalization
- Execution event emission
- TTL-based swarm retention
- Identity binding (swarm_id ↔ job_id)

The router converts raw worker RPC into normalized events:

```
codex_rpc → translate_event() → codeswarm.router.v1
```

Only execution-semantic events are surfaced:

- turn_started
- reasoning_delta
- assistant_delta
- command_started
- command_completed
- usage
- turn_complete
- swarm_removed

Raw payloads are preserved for forward compatibility.

---

### Backend (Reconciliation Mirror)

The backend is not authoritative.

It mirrors router state and:

- Bridges TCP → WebSocket
- Tracks request_id ↔ swarm_id
- Performs authoritative reconciliation on `swarm_list`
- Removes stale swarms
- Forwards normalized events

If the router says a swarm is gone, it is gone.

This rule was learned the hard way.

---

### Frontend (Event-Sourced Execution View)

The frontend is execution-aware.

It renders:

- Per-node turns
- Reasoning streams
- Command execution blocks
- Token usage
- Lifecycle transitions

It performs optimistic UI updates — but reconciles on authoritative rejection.

Launch failure? Ghost removed.
Swarm removed? UI drops it.
No infinite “Launching…” illusions.

Execution > illusion.

---

## 4. Authority and Reconciliation

Distributed systems fail in subtle ways.

Codeswarm learned early:

- SSH timeout ≠ termination
- Slurm `NOT_FOUND` ≠ authoritative deletion
- A single failed probe is not a lifecycle signal

Router authority is singular.
Backend reconciliation is symmetric.
Frontend optimism is temporary.

This layering prevents:

- Zombie swarms
- Lifecycle flapping
- Identity mismatches
- Phantom terminations

---

## 5. Slurm Is Not a Source of Truth

Slurm is a scheduling authority, not a lifecycle oracle.

Cluster signals are eventually consistent.

Early versions treated transient SSH failure as job death.

That was wrong.

The current model:

- Disable aggressive 5s polling
- Avoid treating single probe failures as termination
- Preserve swarm state
- Use TTL-based cleanup (default 15 minutes)

Swarm removal is deliberate — not accidental.

---

## 6. Multi-Node Execution

Each swarm can span multiple nodes (subject to cluster policy).

Workers are launched via SBATCH:

```
#SBATCH --nodes=N
#SBATCH --ntasks=N
```

Each node receives an isolated workspace:

```
runs/<job_id>/node_XX/
```

Parallel injection is supported.

Nodes operate independently.

This is not simulated concurrency.
It is real distributed execution.

---

## 7. Failure Handling Is a Feature

When a 2-node launch fails due to Slurm policy:

- Router emits `command_rejected`
- Backend forwards failure
- Frontend removes launch ghost
- Error is displayed
- No inconsistent state remains

Failure is first-class.

Not swallowed.
Not hidden.
Not retried blindly.

---

## 8. Lessons Learned

### 1. Lifecycle authority must be singular.
Mirrors must mirror. Not merge.

### 2. Polling is weak signal.
Timeouts are not facts.

### 3. Optimistic UI must reconcile.
Illusions are temporary.

### 4. Identity must be immutable.
A swarm_id ↔ job_id mapping must never drift.

### 5. Execution semantics matter more than chat semantics.
Tool calls and reasoning are first-class citizens.

---

## 9. Why This Matters

As models improve, scaling up a single agent will plateau.

The interesting frontier is:

- Many agents
- Coordinated in parallel
- Operating with isolation
- Sharing context strategically
- Running across real infrastructure

Codeswarm is not claiming to solve that future.

It is a testbed.

A control plane.

A sandbox for distributed cognition experiments.

---

## 10. What Codeswarm Is Not

It is not:

- A chatbot wrapper
- A generic workflow engine
- A production orchestration framework (yet)
- A replacement for Kubernetes

It is an experimental execution fabric for multi-agent exploration.

---

## 11. Future Directions

- Worker heartbeat liveness model
- Event-sourced backend persistence
- Injection targeting UI
- Multi-cluster routing
- Swarm composition primitives
- Coordinated agent messaging
- Resource-aware scheduling heuristics

---

## Closing Thought

The industry built chat interfaces because they were easy.

Distributed agent execution is harder.

But the interesting systems problems live there.

Codeswarm exists to explore that terrain.

Sometimes it crashes into Slurm policy limits.
Sometimes it discovers lifecycle edge cases.
Sometimes it behaves beautifully.

That’s the point.
