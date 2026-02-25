# Codeswarm

Codeswarm is a scalable Slurm-based distributed Codex runtime designed for HPC clusters.

It provides:

- Router control plane (JSON stdin/stdout protocol)
- Slurm job orchestration
- Shared filesystem mailbox transport
- Correlated prompt injection
- Streaming assistant responses
- OpenClaw-compatible daemon mode

---

## Quick Start

### 1. Allocate Worker

```
python slurm/allocate_and_prepare.py --config configs/<cluster>.json
```

### 2. Start Router

```
python router/router.py --config configs/<cluster>.json --daemon
```

### 3. Inject Prompt

```json
{"action":"inject","job_id":"<JOB_ID>","node_id":0,"content":"Hello"}
```

---

## Documentation

Full documentation available in:

```
docs/USER_GUIDE.md
```

---

## Cluster Requirements

Codeswarm assumes:

- Shared filesystem between login and compute nodes
- Login node acts as Slurm submission host
- Passwordless SSH configured
- Slurm supports graceful termination signals

See `docs/USER_GUIDE.md` for detailed cluster assumptions and configuration.

---

## Status

Stable daemon mode with:

- Non-blocking injection
- Delivery reporting
- Configurable SSH timeout
- Single SSH scalable follower
- Correlated event lifecycle

Production-ready for OpenClaw integration.
