# Codeswarm Architecture

This document describes the high-level architecture of Codeswarm after the introduction of the Cluster Provider abstraction.

---

# 1. System Overview

Codeswarm consists of three primary layers:

1. **CLI (Intent Layer)**
2. **Router (Control Plane)**
3. **Cluster Provider (Execution Backend)**

```
CLI  →  Router  →  ClusterProvider  →  Compute Backend
```

The router exposes a backend-neutral protocol (`codeswarm.router.v1`).
All backend-specific logic is isolated behind the `ClusterProvider` interface.

---

# 2. CLI (Intent Layer)

Responsibilities:

- Construct protocol-compliant JSON commands
- Manage TCP transport lifecycle
- Format streaming runtime events
- Handle inject lifecycle logic

The CLI does NOT:

- Know about Slurm
- Know about partitions, accounts, or QOS
- Contain backend-specific logic

Launch payloads are backend-neutral:

```json
{
  "nodes": 2,
  "system_prompt": "..."
}
```

---

# 3. Router (Control Plane)

The router is a long-running daemon responsible for:

- Swarm registry and state persistence
- Translating worker runtime RPC into protocol events
- Managing inject lifecycle
- Delegating cluster operations to a provider

The router does NOT:

- Contain Slurm-specific logic
- Contain AWS/Kubernetes logic
- Know backend parameter semantics

---

# 4. Cluster Provider Abstraction

The router delegates execution backend operations to a provider implementing the `ClusterProvider` interface.

```
router/
  cluster/
    base.py
    slurm.py
    factory.py
```

## 4.1 ClusterProvider Interface

Defined in `router/cluster/base.py`:

```python
class ClusterProvider(ABC):
    def launch(self, nodes: int) -> str: ...
    def terminate(self, job_id: str) -> None: ...
    def get_job_state(self, job_id: str) -> Optional[str]: ...
    def list_active_jobs(self) -> Dict[str, str]: ...
```

The interface is backend-neutral.

---

## 4.2 SlurmProvider

Implements `ClusterProvider`.

Responsibilities:

- Generate SBATCH script
- Submit jobs
- Query job state via `squeue`
- Cancel jobs via `scancel`
- Pull Slurm-specific parameters from config

All Slurm parameters live under:

```json
cluster.slurm
```

The router never references partition/account/QOS directly.

---

# 5. Worker Runtime

Workers run inside cluster jobs.

They:

- Read mailbox inbox files
- Emit runtime RPC events
- Produce assistant responses

The router translates worker RPC into protocol events.

---

# 6. Design Principles

1. **Backend Neutral Protocol**
2. **Provider Encapsulation**
3. **Single Source of Configuration**
4. **Deterministic Control Plane**
5. **No Silent Failures**

---

# 7. Extending to New Backends

To add a new backend:

1. Implement `ClusterProvider`
2. Add to `cluster/factory.py`
3. Add backend-specific config section

No protocol changes required.

---

End of Architecture Document.
