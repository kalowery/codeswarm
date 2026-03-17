from abc import ABC, abstractmethod
from typing import Callable, Dict, Optional
import subprocess
from pathlib import Path


class ClusterProvider(ABC):

    @abstractmethod
    def launch(
        self,
        nodes: int,
        agents_md_content: str | None = None,
        agents_bundle: dict | None = None,
        launch_params: dict | None = None,
        progress_cb: Callable[[str, str], None] | None = None,
    ) -> str:
        """Launch swarm and return backend job_id."""
        pass

    @abstractmethod
    def terminate(self, job_id: str, terminate_params: dict | None = None) -> None:
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

    def create_workspace_archive(
        self,
        job_id: str,
        swarm_id: str,
        output_dir: Path,
    ) -> str | None:
        """
        Optional hook used by terminate flow when archive download is requested.
        Return absolute archive path when created, else None.
        """
        return None

    def bind_swarm(self, job_id: str, swarm_id: str, swarm_record: dict) -> None:
        """
        Optional hook invoked after the router assigns a swarm_id to a launched job.
        Providers may persist enough metadata to recover active swarms after router restart.
        """
        return None

    def recover_swarms(self) -> Dict[str, dict]:
        """
        Optional hook invoked during router startup.
        Return recoverable active swarms keyed by swarm_id.
        """
        return {}
