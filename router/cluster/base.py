from abc import ABC, abstractmethod
from typing import Dict, Optional
import subprocess


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

    @abstractmethod
    def start_follower(self) -> subprocess.Popen | None:
        """Return a process streaming worker events via stdout."""
        pass

    @abstractmethod
    def inject(
        self,
        job_id: str,
        node_id: int,
        content: str,
        injection_id: str
    ) -> None:
        """Deliver injection to worker."""
        pass
