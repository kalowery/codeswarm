# Codeswarm

Codeswarm is a provider-agnostic execution system for orchestrating multi-node Codex workers on:

- Local processes (single machine)
- Slurm clusters (HPC)

It provides a router control plane, a backend API/WebSocket bridge, and a Next.js frontend.

## Install via curl | bash

```bash
curl -fsSL https://raw.githubusercontent.com/kalowery/codeswarm/main/install-codeswarm.sh | bash
```

Optional installer overrides:

- `CODESWARM_REPO_URL`
- `CODESWARM_BRANCH`
- `CODESWARM_INSTALL_DIR` (default: `~/.codeswarm`)

## Quick Start (Local)

1. Clone

```bash
git clone https://github.com/kalowery/codeswarm.git
cd codeswarm
```

2. Bootstrap dependencies

```bash
./bootstrap.sh
```

Bootstrap installs Node `24.13.0`, workspace dependencies, builds frontend/CLI, and verifies Codex CLI login.

3. Use local config

`configs/local.json` already exists and uses local backend:

```json
{
  "cluster": {
    "backend": "local",
    "workspace_root": "runs",
    "archive_root": "/tmp/archives"
  }
}
```

4. Start the full web stack

```bash
codeswarm web --config configs/local.json
```

This starts:

- Router on `127.0.0.1:8765`
- Backend on `http://localhost:4000`
- Frontend on `http://localhost:3000`

You can also run components manually:

```bash
python3 -u -m router.router --config configs/local.json --daemon
npm --prefix web/backend run dev
npm --prefix web/frontend run dev
```

## Codex Sandbox and Approval

Codeswarm handles execution approval in its own UI/router flow (`exec_approval_required` -> `/approval` -> router `approve_execution`).

To avoid conflicting prompts and write failures, configure Codex for workspace writes and no internal approval gate:

```toml
sandbox = "workspace-write"
approvalPolicy = "never"
```

Equivalent CLI flags:

```bash
codex --sandbox workspace-write --ask-for-approval never
```

If Codex is left in read-only or on-request modes, commands may execute inconsistently or fail to write files.

## Architecture

```mermaid
flowchart TD
    UI[Frontend Next.js] -->|WebSocket| BE[Backend Express]
    BE -->|TCP codeswarm.router.v1| RT[Router]
    RT --> PR[Provider Interface]
    PR -->|local| LP[LocalProvider]
    PR -->|slurm| SP[SlurmProvider]
    LP --> WK[Workers]
    SP --> WK
    WK --> MB[Mailbox JSONL]
    MB --> RT
```

Core principles:

- Provider abstraction: router is backend-neutral.
- Event-sourced UI: frontend state derives from streamed events.
- Mailbox contract: worker inbox/outbox JSONL files.
- Durable control state: `router_state.json` and backend `state.json`.

## Providers

### Local

- Spawns worker subprocesses.
- Uses mailbox under `<workspace_root>/mailbox` (default `runs/mailbox`).
- Optional archive move on terminate via `cluster.archive_root`.

### Slurm

- Submits jobs through `slurm/allocate_and_prepare.py`.
- Uses SSH (`ssh.login_alias`) for `squeue`, `scancel`, inbox writes, and outbox follower.
- Mailbox under `<workspace_root>/<cluster_subdir>/mailbox`.

## Control Commands

Router command set (protocol `codeswarm.router.v1`):

- `swarm_launch`
- `inject`
- `swarm_list`
- `swarm_status`
- `approve_execution`
- `swarm_terminate`

## Troubleshooting

### Codex not installed

```bash
npm install -g @openai/codex
```

### Codex not logged in

```bash
codex login
```

### Router not reachable

Ensure router daemon is running on port `8765`:

```bash
python3 -u -m router.router --config configs/local.json --daemon
```

### Frontend/Backend build check

```bash
npm --workspace=web/frontend run build
```

## Additional Docs

- `docs/CONFIG_SCHEMA.md`
- `docs/PROTOCOL.md`
- `docs/PROTOCOL_SPEC.md`
- `docs/PROVIDER_INTERFACE.md`
- `docs/USER_GUIDE.md`
