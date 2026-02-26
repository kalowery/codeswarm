# Cluster Provider Interface

This document formally specifies the ClusterProvider interface used by the Codeswarm router.

---

# 1. Purpose

The ClusterProvider abstraction isolates execution backend logic from the router control plane.

The router interacts only with the provider interface and does not contain backend-specific logic.

---

# 2. Interface Definition

```python
class ClusterProvider(ABC):

    def launch(self, nodes: int) -> str:
        """
        Launch a swarm with the specified node count.
        Returns a backend-specific job identifier.
        """

    def terminate(self, job_id: str) -> None:
        """
        Terminate the backend job.
        """

    def get_job_state(self, job_id: str) -> Optional[str]:
        """
        Return backend state string (e.g., RUNNING) or None if not found.
        """

    def list_active_jobs(self) -> Dict[str, str]:
        """
        Return mapping of job_id → backend state.
        """
```

---

# 3. Design Constraints

Providers MUST:

- Be stateless beyond configuration
- Pull backend parameters exclusively from configuration
- Not modify router state directly
- Not emit protocol events directly

Router is responsible for:

- State registry
- Event emission
- Swarm lifecycle tracking

---

# 4. Configuration Responsibility

Provider-specific configuration lives under:

```json
{
  "cluster": {
    "backend": "<provider-name>",
    "<provider-name>": { ... }
  }
}
```

Example (Slurm):

```json
{
  "cluster": {
    "backend": "slurm",
    "slurm": {
      "login_alias": "hpcfund",
      "partition": "mi2508x",
      "time_limit": "00:20:00"
    }
  }
}
```

---

# 5. Future Backends

Examples:

- AWSProvider
- KubernetesProvider
- LocalProvider

Each backend implements the same interface without changing protocol.

---

End of Provider Interface Specification.
