# Codeswarm User Guide

## Overview

Codeswarm is a scalable Slurm-based distributed Codex runtime designed to run across HPC clusters. It provides:

- A router control plane (JSON stdin/stdout protocol)
- Remote Slurm job orchestration
- Shared filesystem mailbox transport
- Correlated prompt injection
- Streaming assistant output
- OpenClaw-compatible daemon mode

---

# Architecture

## Components

### 1. Router (Control Plane)

Runs on the login/OpenClaw host.

Responsibilities:
- Starts remote outbox follower (single SSH session)
- Streams worker events
- Translates Codex RPC events
- Accepts JSON control commands (inject)
- Correlates events via `injection_id`

Daemon mode:

```
python router/router.py --config configs/<cluster>.json --daemon
```

Protocol:

Input:

```json
{"action":"inject","job_id":"272900","node_id":0,"content":"Hello"}
```

Output:

```json
{"type":"inject_ack", ...}
{"type":"inject_delivered", ...}
{"type":"assistant_delta", ...}
{"type":"assistant", ...}
{"type":"turn_complete", ...}
{"type":"usage", ...}
```

---

### 2. Slurm Worker (codex_worker.py)

Runs on compute nodes.

Responsibilities:
- Launch Codex CLI
- Process inbox messages
- Emit structured RPC events
- Archive outbox on shutdown
- Handle SIGTERM gracefully

---

### 3. Outbox Follower

A lightweight remote process that:

- Runs on the login node
- Watches `mailbox/outbox/*.jsonl`
- Streams new lines over a single SSH session

This replaces per-node `tail -F`.

---

# Cluster Assumptions

Codeswarm assumes the following Slurm cluster topology:

## 1. Shared Filesystem

- Login node and compute nodes share a common filesystem
- Mailbox paths are visible from both

Example:

```
/work1/amd/klowery/workspace/codeswarm/
```

Must be accessible from:
- Login node
- All allocated compute nodes

---

## 2. Login Node as Jump Host

- Router connects to login node via SSH
- Login node launches Slurm jobs
- Compute nodes are not directly SSH’d from router

---

## 3. Passwordless SSH

Required:

```
ssh <login_alias>
```

Must work without:
- Password prompt
- MFA prompt
- Interactive confirmation

Recommended SSH config:

```
Host hpcfund
    HostName login.cluster.edu
    User klowery
    IdentityFile ~/.ssh/id_rsa
    BatchMode yes
```

---

## 4. Slurm Configuration

Slurm must support:

- `sbatch`
- `squeue`
- `--signal=TERM@60`

Workers rely on:

```
#SBATCH --signal=TERM@60
```

To allow graceful shutdown and outbox archiving.

---

## 5. Mailbox Layout

```
mailbox/
    inbox/
        <JOB_ID>_<NODE_ID>.jsonl
    outbox/
        <JOB_ID>_<NODE_ID>.jsonl
    archive/
        <JOB_ID>_<NODE_ID>.jsonl
```

Router writes to inbox.
Workers write to outbox.
Workers archive outbox on completion.

---

# Configuration

Example cluster config (`configs/hpcfund.json`):

```json
{
  "ssh": {
    "login_alias": "hpcfund"
  },
  "cluster": {
    "workspace_root": "/work1/amd/klowery/workspace",
    "cluster_subdir": "codeswarm"
  },
  "router": {
    "inject_timeout_seconds": 60
  }
}
```

---

# Running Codeswarm

Codeswarm supports both low-level router usage and the modern CLI workflow.

---

# Modern CLI Workflow (Recommended)

Build and link the CLI from `codeswarm/cli`:

```
npm install
npm run build
npm link
```

## Launch a Swarm

```
codeswarm launch \
  --nodes 4 \
  --prompt "You are a distributed agent." \
  --config configs/hpcfund.json
```

## List Active Swarms

```
codeswarm list --config configs/hpcfund.json
```

## Check Swarm Status

```
codeswarm status <swarm_id> --config configs/hpcfund.json
```

## Inject a Prompt

```
codeswarm inject <swarm_id> \
  --prompt "Optimize GEMM tiling." \
  --config configs/hpcfund.json
```

## Attach and Stream Output

```
codeswarm attach <swarm_id> --config configs/hpcfund.json
```

JSON streaming mode:

```
codeswarm attach <swarm_id> --config configs/hpcfund.json --json
```

## Terminate a Swarm

```
codeswarm terminate <swarm_id> --config configs/hpcfund.json
```

The CLI communicates with the router using the documented JSON protocol and is the preferred interface for production use.

---

# Low-Level Router Workflow (Advanced)

## 1. Allocate and Prepare (Direct Slurm)

```
python slurm/allocate_and_prepare.py --config configs/hpcfund.json
```

This:
- Rsyncs agent directory
- Submits Slurm job

---

## 2. Start Router Daemon

```
python router/router.py --config configs/hpcfund.json --daemon
```

Optional debug mode:

```
--debug
```

---

## 3. Inject Prompt (Raw Protocol)

Example JSON message:

```json
{"command":"inject","swarm_id":"<swarm_id>","nodes":0,"content":"Hello"}
```

---

# Event Model

## Injection Lifecycle

1. inject_ack
2. inject_delivered
3. turn_started
4. assistant_delta (streaming)
5. assistant
6. turn_complete
7. usage

All events include:

- job_id
- node_id
- injection_id

---

# Failure Modes

## Injection Timeout

If SSH is slow:

```json
{"type":"inject_failed","error":"timeout"}
```

Increase timeout via config.

---

## SSH Misconfiguration

Symptoms:
- inject_failed with stderr

Fix:
- Ensure passwordless SSH
- Ensure correct login_alias

---

## No Shared Filesystem

Symptoms:
- Worker never receives injection

Fix:
- Ensure mailbox path visible from both login + compute nodes

---

# OpenClaw Integration

Codeswarm is designed to run as a persistent subprocess.

OpenClaw:
- Spawns router in daemon mode
- Writes JSON to stdin
- Reads JSON from stdout

No OpenClaw modifications required.

---

# Production Recommendations

- Use dedicated service account
- Configure SSH with BatchMode
- Ensure low-latency shared filesystem
- Monitor mailbox directory growth
- Archive old job directories periodically

---

# Summary

Codeswarm provides a scalable, correlated, Slurm-native distributed Codex runtime suitable for:

- HPC environments
- Multi-node distributed jobs
- OpenClaw integration
- Long-running control plane workflows

It assumes a traditional shared-filesystem Slurm architecture with passwordless SSH and login-node orchestration.
