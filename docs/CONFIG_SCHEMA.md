# Codeswarm Configuration Schema

This document reflects the configuration keys currently used by router and providers.

## Top-level structure

```json
{
  "cluster": { ... },
  "ssh": { ... },
  "launch_providers": [ ... ],
  "router": { ... }
}
```

`ssh` is optional. Slurm login routing is configured via `cluster.slurm.login_host` (or profile-specific `cluster.slurm.profiles.<name>.login_host`).

## `cluster`

### Required

- Legacy mode: `cluster.backend`
- Multi-provider mode: `launch_providers` entries with `backend`

### Local backend (`cluster.backend = "local"`)

- `cluster.workspace_root` (optional, default: `runs`)
- `cluster.archive_root` (optional)
- `cluster.local.workspace_root` (optional override)
- `cluster.local.archive_root` (optional override)

Example:

```json
{
  "cluster": {
    "backend": "local",
    "workspace_root": "runs",
    "archive_root": "/tmp/archives"
  }
}
```

### Slurm backend (`cluster.backend = "slurm"`)

Required:

- `cluster.workspace_root` or `cluster.slurm.workspace_root`
- `cluster.cluster_subdir` or `cluster.slurm.cluster_subdir`
- `cluster.slurm.login_host` (or `cluster.slurm.profiles.<name>.login_host`)
- `cluster.slurm.partition`
- `cluster.slurm.time_limit`

Optional:

- `cluster.slurm.account`
- `cluster.slurm.qos`

Example:

```json
{
  "cluster": {
    "backend": "slurm",
    "workspace_root": "/path/to/workspace",
    "cluster_subdir": "codeswarm",
    "slurm": {
      "login_host": "my-cluster",
      "partition": "compute",
      "time_limit": "00:20:00",
      "account": null,
      "qos": null,
      "workspace_root": "/path/to/slurm/workspace",
      "cluster_subdir": "codeswarm"
    }
  }
}
```

### AWS backend (`cluster.backend = "aws"` or AWS launch provider)

Required:

- `cluster.workspace_root` or `cluster.aws.workspace_root`
- `cluster.cluster_subdir` or `cluster.aws.cluster_subdir`
- `cluster.aws.region`
- `cluster.aws.ami_id`
- `cluster.aws.subnet_id`
- `cluster.aws.key_name`
- `cluster.aws.ssh_private_key_path`

### Launch provider presets (`launch_providers`)

Optional but recommended for mixed environments. Each preset includes:

- `id`: UI/provider id (string)
- `label`: UI label (string)
- `backend`: `local` | `slurm` | `aws`
- `cluster_profile` (optional): selects a named backend profile under `cluster.<backend>.profiles`
  - legacy alias: `cluster_config`
- `defaults` (optional): default provider params
- `launch_fields` / `launch_panels` (optional): UI form metadata
- `launch_soft_timeout_seconds` (optional): how long launch can run before UI marks it delayed
- `launch_hard_timeout_seconds` (optional): hard timeout after which launch is marked failed and late materialization is auto-terminated

### Backend profiles (optional)

Each backend config can define multiple named profiles under `profiles`:

```json
{
  "cluster": {
    "aws": {
      "profiles": {
        "default": { "region": "us-east-1", "instance_type": "c7i.4xlarge", "...": "..." },
        "gpu": { "region": "us-east-1", "instance_type": "g6.4xlarge", "...": "..." }
      }
    },
    "slurm": {
      "profiles": {
        "hpcfund": { "login_host": "hpcfund", "partition": "mi2508x", "...": "..." },
        "lab2": { "login_host": "lab2", "partition": "compute", "...": "..." }
      }
    }
  },
  "launch_providers": [
    {
      "id": "aws-cpu",
      "label": "AWS CPU",
      "backend": "aws",
      "cluster_profile": "default",
      "launch_soft_timeout_seconds": 900,
      "launch_hard_timeout_seconds": 2700
    },
    { "id": "aws-gpu", "label": "AWS GPU", "backend": "aws", "cluster_profile": "gpu" },
    { "id": "slurm-hpcfund", "label": "Slurm HPCFund", "backend": "slurm", "cluster_profile": "hpcfund" }
  ]
}
```

## `ssh`

### `ssh.login_alias`

Legacy/deprecated alias for `cluster.slurm.login_host` in older configs.
New configs should use `login_host` under `cluster.slurm` or `cluster.slurm.profiles.<name>`.

## `router`

`router` section is optional. Current keys consumed by router include:

- `inject_timeout_seconds`
- `graceful_terminate_timeout_seconds`
- `graceful_terminate_poll_seconds`
- `local_graceful_terminate_timeout_seconds`

## Validation behavior

Validation is performed by `common/config.py`:

- missing required fields for selected backend raise runtime errors
- unsupported backend in `cluster.backend` or `launch_providers.*.backend` raises runtime error
