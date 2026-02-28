import subprocess
import uuid
import shutil
from pathlib import Path
from typing import Dict, List, Optional


class LocalProvider:
    """
    Local execution provider.
    Spawns agent workers directly on the host machine.
    """

    def __init__(self, config: dict):
        self.config = config or {}
        self.jobs: Dict[str, List[subprocess.Popen]] = {}

        # Root directory for local runs
        self.workspace_root = Path(
            self.config.get("workspace_root", "runs")
        )

        # Archive root (optional)
        self.archive_root = self.config.get("archive_root")

    def launch(self, nodes: int) -> str:
        job_id = f"local_{uuid.uuid4().hex[:8]}"
        procs: List[subprocess.Popen] = []

        for i in range(nodes):
            node_index = f"{i:02d}"
            node_dir = self.workspace_root / job_id / f"node_{node_index}"
            node_dir.mkdir(parents=True, exist_ok=True)

            # Locate worker relative to repository root
            worker_path = (
                Path(__file__).resolve().parents[2]
                / "agent"
                / "codex_worker.py"
            )

            p = subprocess.Popen(
                ["python3", str(worker_path)],
                cwd=str(node_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            procs.append(p)

        self.jobs[job_id] = procs
        return job_id

    def terminate(self, job_id: str) -> None:
        procs = self.jobs.get(job_id, [])
        for p in procs:
            try:
                p.terminate()
            except Exception:
                pass

        self.jobs.pop(job_id, None)

    def archive(self, job_id: str, swarm_id: str) -> None:
        if not self.archive_root:
            return

        runs_dir = self.workspace_root / job_id
        if not runs_dir.exists():
            return

        archive_root = Path(self.archive_root)
        archive_root.mkdir(parents=True, exist_ok=True)

        target = archive_root / f"swarm_{swarm_id}_{job_id}"

        try:
            shutil.move(str(runs_dir), str(target))
        except Exception as e:
            print(f"[archive] LocalProvider failed to archive {swarm_id}: {e}")

    def get_job_state(self, job_id: str) -> Optional[str]:
        procs = self.jobs.get(job_id)
        if not procs:
            return None

        # If any process still running, treat as RUNNING
        for p in procs:
            if p.poll() is None:
                return "RUNNING"

        return "COMPLETED"

    def list_active_jobs(self) -> Dict[str, str]:
        states = {}
        for job_id in list(self.jobs.keys()):
            state = self.get_job_state(job_id)
            if state:
                states[job_id] = state
        return states
