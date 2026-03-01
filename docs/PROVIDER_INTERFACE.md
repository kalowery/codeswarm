# Provider Interface

Codeswarm uses a provider abstraction to isolate cluster-specific behavior from the Router control plane.

The Router interacts only with the Provider interface and must not contain any SSH or Slurm-specific logic.

---

# 1. Provider Contract

A provider must implement the following methods:

```python
class Provider:
    def launch(self, swarm_id, node_count, system_prompt):
        pass

    def inject(self, job_id, node_id, injection_id, content):
        pass

    def terminate(self, job_id):
        pass

    def get_job_state(self, job_id):
        pass

    def archive(self, job_id, swarm_id):
        pass
```

---

# 2. Responsibilities

## 2.1 launch()

- Allocate execution resources
- Start worker processes
- Ensure CODESWARM_* environment variables are exported
- Initialize mailbox directories

Return value must include:

- job_id
- node_count

---

## 2.2 inject()

- Write JSONL entry to worker inbox

Inbox format:

```json
{
  "injection_id": "uuid",
  "content": "prompt text"
}
```

Provider must write to:

```
mailbox/inbox/<job_id>_<node>.jsonl
```

---

## 2.3 terminate()

Terminate the job associated with job_id.

Local provider:
- Kill subprocesses

Slurm provider:
- `scancel` via SSH

The Router does not implement termination logic.

---

## 2.4 get_job_state()

Return current job state.

Return:
- Truthy value if running
- Falsy if terminated or unknown

Router uses this to reconcile swarm status.

---

## 2.5 archive()

Optional best-effort archival hook.

Used to:
- Clean up resources
- Persist logs
- Perform provider-specific post-processing

---

# 3. Worker Environment Contract

Providers must export:

```
CODESWARM_JOB_ID
CODESWARM_NODE_ID
CODESWARM_BASE_DIR
CODESWARM_CODEX_BIN (optional)
```

Workers must not rely on:

- SLURM_*
- SSH configuration
- cluster-specific variables

---

# 4. Local Provider

Execution model:

- Spawn subprocess per node
- Launch `codex_worker.py`
- Codex must be globally installed

Mailbox root:

```
runs/mailbox/
```

---

# 5. Slurm Provider

Execution model:

- SSH to login node
- Submit SBATCH script
- Use `srun` to launch workers
- Export CODESWARM_* variables

Mailbox root:

```
<workspace>/<cluster_subdir>/mailbox/
```

---

# 6. Abstraction Guarantees

Router:

- Does not know about SSH
- Does not know about Slurm
- Delegates all backend-specific behavior

Providers:

- Encapsulate all cluster semantics
- Implement job lifecycle

This separation enables future providers (e.g., Kubernetes) without router modification.
