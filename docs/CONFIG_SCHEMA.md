# Codeswarm Configuration Schema

This document defines the configuration structure currently expected by the router and Slurm provider implementation.

---

# 1. Top-Level Structure

```json
{
  "ssh": { ... },
  "cluster": { ... },
  "router": { ... }
}
```

---

# 2. SSH Section

```json
{
  "ssh": {
    "login_alias": "hpcfund"
  }
}
```

## Fields

### login_alias

Required.

SSH host alias used for:
- Slurm job submission
- squeue queries
- scancel
- Remote outbox follower

This must correspond to a valid SSH configuration entry (e.g., in `~/.ssh/config`).

---

# 3. Cluster Section

```json
{
  "cluster": {
    "workspace_root": "/path/on/cluster",
    "cluster_subdir": "codeswarm",

    "slurm": {
      "partition": "compute",
      "time_limit": "00:20:00",
      "account": null,
      "qos": null
    }
  }
}
```

## Fields

### workspace_root

Base directory on the shared cluster filesystem.

### cluster_subdir

Subdirectory under `workspace_root` containing Codeswarm runtime files.

### slurm.partition

Required. Slurm partition used for job allocation.

### slurm.time_limit

Required. Slurm time limit (HH:MM:SS).

### slurm.account

Optional. Slurm account string.

### slurm.qos

Optional. Slurm QoS string.

---

# 4. Router Section

```json
{
  "router": {
    "inject_timeout_seconds": 60
  }
}
```

## Fields

### inject_timeout_seconds

Maximum time the router waits for an injection to complete before emitting an `inject_failed` event.

---

# 5. Invariants

- SSH configuration must allow passwordless login.
- Login node and compute nodes must share a filesystem.
- Router state is persisted to `router_state.json`.
- Slurm job discovery during router startup uses `squeue` over SSH.

---

End of Configuration Schema Document.
