from abc import ABC, abstractmethod
from typing import Dict, Optional


class ClusterProvider(ABC):

    @abstractmethod
    def launch(self, nodes: int) -> str:
        """Launch swarm and return backend job_id."""
        pass

    @abstractmethod
    def terminate(self, job_id: str) -> None:
        pass

    @abstractmethod
    def get_job_state(self, job_id: str) -> Optional[str]:
        """Return backend state string or None if not found."""
        pass

    @abstractmethod
    def list_active_jobs(self) -> Dict[str, str]:
        """Return mapping of job_id -> state."""
        pass
