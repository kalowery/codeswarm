# Codeswarm Configuration Schema

This document defines the configuration structure expected by the router and cluster providers.

---

# 1. Top-Level Structure

```json
{
  "cluster": { ... },
  "ssh": { ... },
  "router": { ... }
}
```

---

# 2. Cluster Section

```json
{
  "cluster": {
    "backend": "slurm",
    "workspace_root": "/path/on/cluster",
    "cluster_subdir": "codeswarm",

    "slurm": {
      "login_alias": "hpcfund",
      "partition": "mi2508x",
      "time_limit": "00:20:00",
      "account": "research",
      "qos": "optional"
    }
  }
}
```

## Fields

### backend

String. Name of cluster provider.

### workspace_root

Base directory on remote cluster.

### cluster_subdir

Subdirectory containing Codeswarm runtime files.

---

# 3. SSH Section

```json
{
  "ssh": {
    "login_alias": "hpcfund",
    "controlpersist_minutes": 10
  }
}
```

Used by router and allocation script for remote execution.

---

# 4. Router Section

```json
{
  "router": {
    "ssh_max_workers": 6,
    "heartbeat_timeout_seconds": 60
  }
}
```

Defines router behavior.

---

# 5. Backend-Specific Configuration

Each backend defines its own sub-object under `cluster`.

Example for future AWS:

```json
{
  "cluster": {
    "backend": "aws",
    "aws": {
      "region": "us-east-1",
      "instance_type": "p4d.24xlarge"
    }
  }
}
```

---

# 6. Invariants

- Protocol remains backend-neutral.
- CLI does not expose backend-specific flags.
- All backend parameters are config-driven.

---

End of Configuration Schema Document.
