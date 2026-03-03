# Provider Interface

Codeswarm router is provider-agnostic and delegates backend-specific behavior to `ClusterProvider` implementations.

Current implementations:

- `router/providers/local_provider.py` (`LocalProvider`)
- `router/cluster/slurm.py` (`SlurmProvider`)

## 1. Core provider contract

`ClusterProvider` (`router/cluster/base.py`) defines:

```python
class ClusterProvider(ABC):
    def launch(self, nodes: int) -> str: ...
    def terminate(self, job_id: str) -> None: ...
    def get_job_state(self, job_id: str) -> Optional[str]: ...
    def list_active_jobs(self) -> Dict[str, str]: ...
    def start_follower(self) -> subprocess.Popen | None: ...
    def inject(self, job_id: str, node_id: int, content: str, injection_id: str) -> None: ...
```

In addition, current router behavior expects providers to expose:

- `send_control(job_id, node_id, message)` for approval/control payload delivery
- `archive(job_id, swarm_id)` for best-effort post-termination archival

Both local and slurm providers currently implement these methods.

## 2. Responsibilities

### `launch(nodes)`

- allocate/start workers
- return backend `job_id`

### `inject(job_id, node_id, content, injection_id)`

Append user message to per-node inbox as JSON line:

```json
{
  "type": "user",
  "content": "prompt text",
  "injection_id": "uuid"
}
```

### `send_control(job_id, node_id, message)`

Append control message to per-node inbox:

```json
{
  "type": "control",
  "payload": { ... }
}
```

Used for execution approval responses and other control-plane actions.

### `start_follower()`

Return process that streams worker outbox events on stdout as JSON lines.

### `get_job_state(job_id)` and `list_active_jobs()`

Used for swarm status and startup reconciliation.

### `terminate(job_id)`

Terminate backend job/processes.

### `archive(job_id, swarm_id)`

Best-effort cleanup/archive hook after termination.

## 3. Mailbox conventions

### Local backend

Mailbox under `<workspace_root>/mailbox` (default `runs/mailbox`):

- `inbox/<job_id>_<node>.jsonl`
- `outbox/...`

### Slurm backend

Mailbox under `<workspace_root>/<cluster_subdir>/mailbox`:

- `inbox/<job_id>_<node>.jsonl`
- `outbox/...`

## 4. Worker environment contract

Providers should set:

- `CODESWARM_JOB_ID`
- `CODESWARM_NODE_ID`
- `CODESWARM_BASE_DIR`
- `CODESWARM_CODEX_BIN` (optional)

Workers should not depend on Slurm-specific env vars for control-plane behavior.

## 5. Design guarantees

Router remains free of backend-specific mechanics (SSH/Slurm details live in providers), enabling additional providers without router command-protocol changes.

