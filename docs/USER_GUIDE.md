# Codeswarm User Guide

This guide explains how to run and use Codeswarm in both Local and Slurm modes.

---

# 1. Running Codeswarm

## 1.1 Local Mode (Single Machine)

Local mode runs workers as subprocesses on your machine.

### Requirements

- `codex` installed globally
- Authenticated Codex session

Authenticate:

```bash
printenv OPENAI_API_KEY | codex login --with-api-key
```

Start router:

```bash
python -m router.router --config configs/local.json --daemon
```

Then start backend + frontend.

Mailbox directory:

```
runs/mailbox/
```

---

## 1.2 Slurm Mode (HPC Cluster)

Slurm mode runs workers on a cluster via SBATCH.

### Requirements

- SSH login alias configured
- Slurm cluster access

Start router:

```bash
python -m router.router --config configs/hpcfund.json --daemon
```

Mailbox directory:

```
<workspace>/<cluster_subdir>/mailbox/
```

---

# 2. Swarms

A swarm consists of:

- 1–N nodes
- Independent Codex workers
- Shared swarm identity

Each node runs in isolation.

---

# 3. Injecting Prompts

Use the input box at the bottom of the UI.

## Default

Sends prompt to active node.

## Target All Nodes

```
/all your prompt here
```

## Target Specific Node

```
/node[3] your prompt here
```

---

# 4. Node Navigation (HPC Scale)

The node selector supports:

- Fixed-size node tabs
- Horizontal scrolling
- Left/right navigation arrows
- No shrinking below readable width

Works for 1–128+ nodes.

---

# 5. Attention Indicators

Codeswarm derives attention state automatically.

A node shows a pulsing amber dot when:

- Its last turn has completed
- It is not the currently active node

A swarm shows a pulsing indicator when:

- Any node within it requires attention

Indicators disappear automatically when you view the node.

---

# 6. Streaming Output

Each turn shows:

- User prompt
- Live reasoning (expandable)
- Command executions
- Assistant output
- Token usage

Streaming cursor indicates active generation.

---

# 7. Terminating a Swarm

Click "Terminate" in the swarm header.

Local mode:
- Kills subprocesses

Slurm mode:
- Issues `scancel` via provider

Router handles termination uniformly across providers.

---

# 8. Troubleshooting

## Local: 401 Unauthorized

Ensure Codex is authenticated:

```bash
printenv OPENAI_API_KEY | codex login --with-api-key
```

## Slurm: Injection Fails

Ensure SSH login alias works and cluster access is valid.

---

# 9. Best Practices

- Use Local mode for development.
- Use Slurm for large-scale swarms.
- Keep node counts manageable for cognitive clarity.
- Monitor attention indicators for human-in-the-loop tasks.

---

For architecture details, see:

- `ARCHITECTURE.md`
- `PROVIDER_INTERFACE.md`
