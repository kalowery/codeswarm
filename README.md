# Codeswarm

Codeswarm is a backend-neutral distributed agent runtime with a pluggable cluster execution layer.

It provides a versioned TCP control plane, multi-swarm orchestration, deterministic injection lifecycle management, and streaming agent responses across distributed compute backends.

Codeswarm separates:

- **Control Plane** (Router)
- **Intent Layer** (CLI)
- **Execution Backend** (ClusterProvider)

The execution backend is abstracted, enabling support for Slurm today and additional providers (AWS, Kubernetes, etc.) in the future.

---

# Architecture

```
CLI (Node.js)
   ↓ TCP (codeswarm.router.v1)
Router (Python daemon)
   ↓
ClusterProvider (pluggable backend)
   ↓
Execution Backend (e.g. Slurm)
   ↓
Workers (distributed agents)
```

## Core Components

- **CLI** – User-facing control client (`codeswarm`)
- **Router** – Persistent control-plane daemon
- **ClusterProvider** – Backend abstraction interface
- **Provider Implementation** – e.g. `SlurmProvider`
- **Worker Runtime** – Distributed agent processes

The router never contains backend-specific logic.
All execution details are encapsulated in provider implementations.

---

# Control Plane

Transport:

- Local TCP (`127.0.0.1:8765` by default)
- Newline-delimited JSON (NDJSON)
- Versioned protocol: `codeswarm.router.v1`

The CLI communicates exclusively via this protocol.

All commands are correlated by `request_id`.
All runtime events include `swarm_id`, `node_id`, and `injection_id`.

---

# Multi-Swarm Model

Codeswarm supports multiple concurrent swarms.

Each swarm has:

- `swarm_id` (control-plane identifier)
- `job_id` (backend identifier)
- `node_count`
- `system_prompt`
- `status`

The router maintains authoritative swarm state.
Backends are queried via the provider abstraction.

---

# Quick Start (CLI)

From `codeswarm/cli`:

```bash
npm install
npm run build
npm link
```

Launch a swarm:

```bash
codeswarm launch \
  --nodes 4 \
  --prompt "You are a focused autonomous agent." \
  --config ../configs/hpcfund.json
```

Inject into swarm:

```bash
codeswarm inject <swarm_id> \
  --prompt "Optimize GEMM tiling." \
  --config ../configs/hpcfund.json
```

Check status:

```bash
codeswarm status <swarm_id> \
  --config ../configs/hpcfund.json
```

List active swarms:

```bash
codeswarm list \
  --config ../configs/hpcfund.json
```

Terminate a swarm:

```bash
codeswarm terminate <swarm_id> \
  --config ../configs/hpcfund.json
```

Backend-specific parameters (partition, instance type, etc.) are defined in configuration, not in CLI flags.

---

# Cluster Provider Abstraction

The router delegates execution to a provider implementing:

```python
class ClusterProvider:
    def launch(self, nodes: int) -> str
    def terminate(self, job_id: str) -> None
    def get_job_state(self, job_id: str) -> Optional[str]
    def list_active_jobs(self) -> Dict[str, str]
```

This abstraction allows:

- Slurm-based HPC execution
- Future AWS execution
- Future Kubernetes execution
- Local development providers

No protocol changes are required to add new backends.

---

# Example Backend: Slurm

The current production provider is `SlurmProvider`.

It:

- Generates SBATCH scripts
- Submits jobs
- Queries job state via `squeue`
- Cancels jobs via `scancel`

All Slurm parameters are defined under:

```json
{
  "cluster": {
    "backend": "slurm",
    "slurm": { ... }
  }
}
```

The router core remains unaware of Slurm-specific flags.

---

# Transport Guarantees

- Deterministic TCP handshake with retry
- Buffered JSON framing
- No stdio IPC
- No UNIX sockets
- Structured error propagation (`command_rejected`)
- Client lifecycle management

---

# Design Principles

1. Backend-neutral protocol
2. Provider encapsulation
3. Deterministic lifecycle management
4. No silent failures
5. Router as authoritative control plane

---

# Status (2026‑02‑26)

Stable:

- Backend-neutral ClusterProvider abstraction
- SlurmProvider implementation
- Multi-swarm orchestration
- Deterministic inject lifecycle
- Structured protocol documentation
- CLI UX polish (`codeswarm` binary)

Ready for:

- Additional backend providers
- Web UI reuse over same protocol
- OpenClaw integration
- Production distributed workflows

---

# Documentation

- CLI docs: `cli/README.md`
- Architecture: `docs/ARCHITECTURE.md`
- Protocol spec: `docs/PROTOCOL.md`
- Provider interface: `docs/PROVIDER_INTERFACE.md`
- Config schema: `docs/CONFIG_SCHEMA.md`
