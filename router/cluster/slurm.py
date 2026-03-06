import subprocess
import re
import json
import shlex
import base64
from pathlib import Path
from typing import Callable, Dict, Optional

from .base import ClusterProvider


class SlurmProvider(ClusterProvider):

    def __init__(self, config: dict):
        self.config = config
        self.cluster_cfg = config.get("cluster", {})
        self.slurm_cfg = self.cluster_cfg.get("slurm", {})

    def launch(
        self,
        nodes: int,
        agents_md_content: str | None = None,
        launch_params: dict | None = None,
        progress_cb: Callable[[str, str], None] | None = None,
    ) -> str:
        def _progress(stage: str, message: str):
            if callable(progress_cb):
                try:
                    progress_cb(stage, message)
                except Exception:
                    pass

        _progress("starting", f"Preparing Slurm launch for {nodes} node(s)")
        config_path = self.config.get("_config_path")
        if not config_path:
            raise RuntimeError("Router config path not available for swarm launch")

        launch_params = launch_params if isinstance(launch_params, dict) else {}
        partition = launch_params.get("partition") or self.slurm_cfg.get("partition")
        time_limit = launch_params.get("time_limit") or self.slurm_cfg.get("time_limit")
        account = launch_params.get("account") if "account" in launch_params else self.slurm_cfg.get("account")
        qos = launch_params.get("qos") if "qos" in launch_params else self.slurm_cfg.get("qos")
        if isinstance(account, str) and not account.strip():
            account = None
        if isinstance(qos, str) and not qos.strip():
            qos = None

        if not partition:
            raise RuntimeError("Slurm partition not configured")
        _progress("config", f"Using partition: {partition}")

        if not time_limit:
            raise RuntimeError("Slurm time_limit not configured")
        _progress("config", f"Using time limit: {time_limit}")

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
        if agents_md_content is not None and agents_md_content.strip():
            agents_md_b64 = base64.b64encode(
                agents_md_content.encode("utf-8")
            ).decode("ascii")
            cmd += ["--agents-md-b64", agents_md_b64]

        _progress("submitting", "Running Slurm allocate and prepare script")
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        output_lines = []
        if proc.stdout is not None:
            for raw in proc.stdout:
                output_lines.append(raw)
                line = raw.strip()
                if line:
                    _progress("slurm_setup", line)

        exit_code = proc.wait()
        output = "".join(output_lines)

        if exit_code != 0:
            raise RuntimeError(
                f"Swarm launch failed (exit {exit_code}).\n"
                f"OUTPUT:\n{output}"
            )

        match = re.search(r"JOB_ID=(\d+)", output)
        if not match:
            match = re.search(r"Submitted job (\d+)", output)

        if not match:
            raise RuntimeError(f"Unable to parse Slurm JOB_ID. Output:\n{output}")

        _progress("ready", f"Slurm job is ready: {match.group(1)}")

        return match.group(1)

    def terminate(self, job_id: str, terminate_params: dict | None = None) -> None:
        login_alias = self.config.get("ssh", {}).get("login_alias")
        if not login_alias:
            raise RuntimeError("SSH login_alias not configured")

        subprocess.run(["ssh", login_alias, f"scancel {job_id}"])

    def archive(self, job_id: str, swarm_id: str) -> None:
        # Archival for Slurm should be handled by cluster-side policy
        # (e.g., SBATCH epilog or shared filesystem rules).
        # Router does not enforce filesystem moves for Slurm backend.
        return

    def get_job_state(self, job_id: str) -> Optional[str]:
        login_alias = self.config.get("ssh", {}).get("login_alias")
        if not login_alias:
            raise RuntimeError("SSH login_alias not configured")

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
        login_alias = self.config.get("ssh", {}).get("login_alias")
        if not login_alias:
            raise RuntimeError("SSH login_alias not configured")

        cmd = [
            "ssh",
            login_alias,
            "squeue -h -o '%i|%j|%T'"
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)

        running_jobs: Dict[str, str] = {}

        for line in result.stdout.splitlines():
            parts = line.strip().split("|")
            if len(parts) != 3:
                continue
            job_id, job_name, state = parts
            running_jobs[job_id] = state

        return running_jobs

    def start_follower(self):
        # Defer to router SSH-based follower
        from ..router import start_remote_follower
        return start_remote_follower(self.config)

    def inject(self, job_id, node_id, content, injection_id):
        login_alias = self.config["ssh"]["login_alias"]
        workspace_root = self.config["cluster"]["workspace_root"]
        cluster_subdir = self.config["cluster"]["cluster_subdir"]

        inbox_path = (
            f"{workspace_root}/{cluster_subdir}/mailbox/inbox/"
            f"{job_id}_{int(node_id):02d}.jsonl"
        )

        payload = {
            "type": "user",
            "content": content,
            "injection_id": injection_id
        }

        json_line = json.dumps(payload)
        remote_cmd = f"printf '%s\\n' {shlex.quote(json_line)} >> {inbox_path}"

        result = subprocess.run(
            ["ssh", login_alias, remote_cmd],
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip())

    def send_control(self, job_id: str, node_id: int, message: dict) -> None:
        """
        Send control message (e.g., exec_approval_response) to a specific worker node
        via SSH, mirroring the inject() path.
        """
        login_alias = self.config["ssh"]["login_alias"]
        workspace_root = self.config["cluster"]["workspace_root"]
        cluster_subdir = self.config["cluster"]["cluster_subdir"]

        inbox_path = (
            f"{workspace_root}/{cluster_subdir}/mailbox/inbox/"
            f"{job_id}_{int(node_id):02d}.jsonl"
        )

        payload = {
            "type": "control",
            "payload": message
        }

        json_line = json.dumps(payload)
        remote_cmd = f"printf '%s\\n' {shlex.quote(json_line)} >> {inbox_path}"

        result = subprocess.run(
            ["ssh", login_alias, remote_cmd],
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip())
