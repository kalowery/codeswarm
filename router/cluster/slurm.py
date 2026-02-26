import subprocess
import re
from pathlib import Path
from typing import Dict, Optional

from .base import ClusterProvider


class SlurmProvider(ClusterProvider):

    def __init__(self, config: dict):
        self.config = config
        self.cluster_cfg = config.get("cluster", {})
        self.slurm_cfg = self.cluster_cfg.get("slurm", {})

    def launch(self, nodes: int) -> str:
        config_path = self.config.get("_config_path")
        if not config_path:
            raise RuntimeError("Router config path not available for swarm launch")

        partition = self.slurm_cfg.get("partition")
        time_limit = self.slurm_cfg.get("time_limit")
        account = self.slurm_cfg.get("account")
        qos = self.slurm_cfg.get("qos")

        if not partition:
            raise RuntimeError("Slurm partition not configured")

        if not time_limit:
            raise RuntimeError("Slurm time_limit not configured")

        repo_root = Path(__file__).resolve().parents[2]
        allocate_script = repo_root / "slurm" / "allocate_and_prepare.py"

        cmd = [
            "python3",
            str(allocate_script),
            "--config",
            config_path,
            "--nodes",
            str(nodes),
            "--time",
            str(time_limit),
            "--partition",
            str(partition),
            "--launch-codex-run",
        ]

        if account:
            cmd += ["--account", str(account)]

        if qos:
            cmd += ["--qos", str(qos)]

        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode != 0:
            raise RuntimeError(
                f"Swarm launch failed (exit {result.returncode}).\n"
                f"STDOUT:\n{result.stdout}\n"
                f"STDERR:\n{result.stderr}"
            )

        output = result.stdout + result.stderr

        match = re.search(r"JOB_ID=(\d+)", output)
        if not match:
            match = re.search(r"Submitted job (\d+)", output)

        if not match:
            raise RuntimeError(f"Unable to parse Slurm JOB_ID. Output:\n{output}")

        return match.group(1)

    def terminate(self, job_id: str) -> None:
        login_alias = self.slurm_cfg.get("login_alias")
        if not login_alias:
            raise RuntimeError("Slurm login_alias not configured")

        subprocess.run(["ssh", login_alias, f"scancel {job_id}"])

    def get_job_state(self, job_id: str) -> Optional[str]:
        login_alias = self.slurm_cfg.get("login_alias")
        if not login_alias:
            raise RuntimeError("Slurm login_alias not configured")

        result = subprocess.run(
            ["ssh", login_alias, f"squeue -j {job_id} -h -o '%T'"],
            capture_output=True,
            text=True,
            timeout=15
        )

        state = result.stdout.strip()
        if not state:
            return None

        return state

    def list_active_jobs(self) -> Dict[str, str]:
        login_alias = self.slurm_cfg.get("login_alias")
        if not login_alias:
            raise RuntimeError("Slurm login_alias not configured")

        cmd = ["ssh", login_alias, "squeue -h -o '%i|%j|%T'"]
        result = subprocess.run(cmd, capture_output=True, text=True)

        running_jobs: Dict[str, str] = {}

        for line in result.stdout.splitlines():
            parts = line.strip().split("|")
            if len(parts) != 3:
                continue
            job_id, job_name, state = parts
            running_jobs[job_id] = state

        return running_jobs
