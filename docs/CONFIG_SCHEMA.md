# Codeswarm Configuration Schema

This document reflects the configuration keys currently used by router and providers.

## Top-level structure

```json
{
  "cluster": { ... },
  "ssh": { ... },
  "router": { ... }
}
```

`ssh` is required for `slurm` backend and optional for `local` backend.

## `cluster`

### Required

- `cluster.backend`: `"local"` or `"slurm"`

### Local backend (`cluster.backend = "local"`)

- `cluster.workspace_root` (optional, default: `runs`)
- `cluster.archive_root` (optional)

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
- `cluster.slurm.partition`
- `cluster.slurm.time_limit`
- `ssh.login_alias`

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
      "partition": "compute",
      "time_limit": "00:20:00",
      "account": null,
      "qos": null,
      "workspace_root": "/path/to/slurm/workspace",
      "cluster_subdir": "codeswarm"
    }
  },
  "ssh": {
    "login_alias": "my-cluster"
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

## `ssh`

### `ssh.login_alias`

Required for slurm backend. Used for:

- `squeue` state checks
- `scancel` termination
- remote inbox writes
- remote outbox follower launch

Other `ssh.*` keys may exist (for operator use), but current router/provider code only consumes `login_alias`.

## `router`

`router` section is currently optional and not enforced by the active router code path.

`router.*` values may exist in configs for future use and are ignored unless consumed by newer code.

## Validation behavior

Validation is performed by `common/config.py`:

- missing required fields for selected backend raise runtime errors
- unsupported `cluster.backend` raises runtime error
