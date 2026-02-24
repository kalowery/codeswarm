# codeswarm

Distributed, interactive Codex execution on Slurm-based HPC clusters.

codeswarm turns a Slurm allocation into a fleet of persistent Codex app-server agents, each running on its own compute node, controllable from outside the cluster via a filesystem-backed message bus.

---

## Architecture Overview

```
OpenClaw (external control plane)
        ↓
Router (protocol translator)
        ↓
Shared Filesystem Mailbox (JSONL)
        ↓
Worker (one per Slurm node)
        ↓
Codex `app-server` (JSON-RPC over stdio)
```

### Key Properties

- ✅ No inbound networking to cluster required
- ✅ No daemons on login node
- ✅ Fully Slurm-driven lifecycle (`sbatch`)
- ✅ Persistent multi-turn Codex sessions per node
- ✅ Structured JSON-RPC (no PTY scraping)
- ✅ Streaming assistant deltas
- ✅ Token usage accounting
- ✅ Scales to multi-node orchestration

---

## Why `app-server` Mode?

Early prototypes used interactive TTY mode and PTY scraping. That approach was fragile due to:

- ANSI redraw behavior
- Character-by-character streaming
- UI-oriented output
- No stable turn boundaries

codeswarm instead uses:

```
codex app-server --listen stdio://
```

This provides:

- Structured JSON-RPC 2.0
- LSP-style initialization handshake
- Explicit `thread/start` and `turn/start`
- Streaming `agent_message_content_delta`
- Deterministic lifecycle events

This makes Codex a programmable distributed runtime rather than a scraped CLI.

---

## Directory Layout

```
codeswarm/
  agent/
    codex_worker.py          # JSON-RPC relay worker (runs under Slurm)
  router/
    router.py                # Outbox streamer + protocol translator
  slurm/
    allocate_and_prepare.py  # Slurm job launcher
  common/
    config.py                # Config loading
  mailbox/
    inbox/                   # Router → worker
    outbox/                  # Worker → router
  tools/
    node/                    # Bootstrapped Node runtime
    npm-global/              # Isolated npm prefix (contains codex CLI)
```

All HPC-side execution occurs under:

```
<workspace_root>/<cluster_subdir>/
```

---

## Worker Lifecycle

Each worker:

1. Launches `codex app-server`
2. Performs LSP handshake:
   - `initialize`
   - `initialized`
3. Calls `thread/start`
4. Waits for user input in mailbox
5. On inbox message:
   - Calls `turn/start`
6. Streams JSON-RPC events to outbox

Workers are persistent for the life of the Slurm job.

---

## Router Responsibilities

The router:

- Polls (currently) or streams outbox files
- Translates `codex/event/*` into OpenClaw-friendly events
- Injects user messages into inbox

Future improvement:

- Replace polling with persistent SSH `tail -F`

---

## Message Bus Design

Mailbox files are append-only JSONL:

### Inbox (Router → Worker)
```
mailbox/inbox/<JOB_ID>_<NODE_ID>.jsonl
```

Example:
```json
{"type":"user","content":"Say hello."}
```

### Outbox (Worker → Router)
```
mailbox/outbox/<JOB_ID>_<NODE_ID>.jsonl
```

Contains raw JSON-RPC events from Codex app-server.

---

## Slurm Execution Model

Jobs are allocated via:

```
sbatch
```

Each node runs:

```
python3 agent/codex_worker.py
```

One Codex session per node.

---

## Authentication

Codex CLI v0.104.0 uses stored credentials under:

```
~/.codex
```

Authentication must be established with:

```
codex login
```

Environment variables like `OPENAI_API_KEY` are not used by this CLI version.

---

## Current Status

✅ LSP handshake working
✅ thread/start working
✅ turn/start working
✅ Streaming deltas working
✅ Token accounting working
✅ Multi-turn capable

Next steps:

- Clean router event translator
- Persistent SSH streaming
- Multi-node orchestration
- OpenClaw channel integration

---

## Design Principles

- Deterministic > clever
- Structured protocols > scraped UI
- Explicit configuration > defaults
- Isolation of toolchain under workspace
- Slurm-native lifecycle
- No cluster-side daemons

---

codeswarm turns Codex into a distributed, programmable HPC-native agent runtime.
