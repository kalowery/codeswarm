import subprocess
import re
import json
import shlex
import base64
import tempfile
from pathlib import Path
from typing import Callable, Dict, Optional

from .base import ClusterProvider


class SlurmProvider(ClusterProvider):

    def __init__(self, config: dict):
        self.config = config
        self.cluster_cfg = config.get("cluster", {})
        self.slurm_cfg = self.cluster_cfg.get("slurm", {})
        self._provider_ref = str(config.get("_provider_ref") or "slurm")

    def _login_host(self) -> str:
        slurm_login = self.slurm_cfg.get("login_host")
        if isinstance(slurm_login, str) and slurm_login.strip():
            return slurm_login.strip()
        legacy = self.slurm_cfg.get("login_alias")
        if isinstance(legacy, str) and legacy.strip():
            return legacy.strip()
        raise RuntimeError("Slurm login_host not configured in cluster.slurm")

    def launch(
        self,
        nodes: int,
        agents_md_content: str | None = None,
        agents_bundle: dict | None = None,
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
        config_path = None
        temp_config_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                suffix=f".{self._provider_ref.replace(':', '_')}.json",
                prefix="codeswarm-slurm-",
                delete=False,
            ) as tf:
                json.dump(self.config, tf)
                temp_config_path = tf.name
            config_path = temp_config_path

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
            if isinstance(agents_bundle, dict):
                try:
                    bundle_payload = json.dumps(agents_bundle, separators=(",", ":"))
                    cmd += [
                        "--agents-bundle-b64",
                        base64.b64encode(bundle_payload.encode("utf-8")).decode("ascii"),
                    ]
                except Exception:
                    pass

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
        finally:
            if temp_config_path:
                try:
                    Path(temp_config_path).unlink(missing_ok=True)
                except Exception:
                    pass

    def terminate(self, job_id: str, terminate_params: dict | None = None) -> None:
        login_host = self._login_host()

        subprocess.run(["ssh", login_host, f"scancel {job_id}"])

    def archive(self, job_id: str, swarm_id: str) -> None:
        # Archival for Slurm should be handled by cluster-side policy
        # (e.g., SBATCH epilog or shared filesystem rules).
        # Router does not enforce filesystem moves for Slurm backend.
        return

    def create_workspace_archive(self, job_id: str, swarm_id: str, output_dir: Path) -> str | None:
        login_host = self._login_host()

        cluster_cfg = self.config.get("cluster", {})
        slurm_cfg = cluster_cfg.get("slurm", {}) if isinstance(cluster_cfg, dict) else {}
        workspace_root = str(slurm_cfg.get("workspace_root") or cluster_cfg.get("workspace_root") or "").rstrip("/")
        cluster_subdir = str(slurm_cfg.get("cluster_subdir") or cluster_cfg.get("cluster_subdir") or "").strip("/")
        if not workspace_root or not cluster_subdir:
            raise RuntimeError("Missing Slurm workspace_root/cluster_subdir for archive export")

        base = f"{workspace_root}/{cluster_subdir}"
        output_dir.mkdir(parents=True, exist_ok=True)
        archive_path = output_dir / f"swarm_{swarm_id}_{job_id}_workspaces.tar.gz"

        remote_script = f"""
set -euo pipefail
BASE={shlex.quote(base)}
JOB={shlex.quote(str(job_id))}
TMP=$(mktemp -d)
ROOT="$TMP/export"
mkdir -p "$ROOT"
FOUND=0

if [ -d "$BASE/runs/$JOB" ]; then
  mkdir -p "$ROOT/runs"
  cp -a "$BASE/runs/$JOB" "$ROOT/runs/"
  FOUND=1
fi

for bucket in inbox outbox archive; do
  SRC="$BASE/mailbox/$bucket"
  if [ ! -d "$SRC" ]; then
    continue
  fi
  mkdir -p "$ROOT/mailbox/$bucket"
  found_bucket=0
  while IFS= read -r -d '' f; do
    cp -a "$f" "$ROOT/mailbox/$bucket/"
    found_bucket=1
    FOUND=1
  done < <(find "$SRC" -maxdepth 1 -type f -name "${{JOB}}_*.jsonl" -print0)
  if [ "$found_bucket" -eq 0 ]; then
    rmdir "$ROOT/mailbox/$bucket" 2>/dev/null || true
  fi
done

if [ "$FOUND" -eq 0 ]; then
  rm -rf "$TMP"
  exit 3
fi

tar -C "$ROOT" -czf - .
rm -rf "$TMP"
"""

        cmd = ["ssh", login_host, "/bin/bash -lc " + shlex.quote(remote_script)]
        with open(archive_path, "wb") as out_f:
            proc = subprocess.Popen(cmd, stdout=out_f, stderr=subprocess.PIPE)
            _, stderr = proc.communicate()

        if proc.returncode == 3:
            archive_path.unlink(missing_ok=True)
            return None
        if proc.returncode != 0:
            archive_path.unlink(missing_ok=True)
            err = (stderr or b"").decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"Failed to export Slurm workspace archive: {err}")
        if archive_path.stat().st_size == 0:
            archive_path.unlink(missing_ok=True)
            return None

        return str(archive_path.resolve())

    def get_job_state(self, job_id: str) -> Optional[str]:
        login_host = self._login_host()

        result = subprocess.run(
            ["ssh", login_host, f"squeue -j {job_id} -h -o '%T'"],
            capture_output=True,
            text=True,
            timeout=15
        )

        state = result.stdout.strip()
        if not state:
            return None

        return state

    def list_active_jobs(self) -> Dict[str, str]:
        login_host = self._login_host()

        cmd = [
            "ssh",
            login_host,
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

    def _resolve_slurm_mailbox_base(self) -> str:
        cluster_cfg = self.config.get("cluster", {})
        slurm_cfg = cluster_cfg.get("slurm", {}) if isinstance(cluster_cfg, dict) else {}

        workspace_root = str(
            slurm_cfg.get("workspace_root") or cluster_cfg.get("workspace_root") or ""
        ).rstrip("/")
        cluster_subdir = str(
            slurm_cfg.get("cluster_subdir") or cluster_cfg.get("cluster_subdir") or ""
        ).strip("/")

        if not workspace_root or not cluster_subdir:
            raise RuntimeError("Missing Slurm workspace_root/cluster_subdir")

        return f"{workspace_root}/{cluster_subdir}"

    def inject(self, job_id, node_id, content, injection_id):
        login_host = self._login_host()
        base = self._resolve_slurm_mailbox_base()

        inbox_path = (
            f"{base}/mailbox/inbox/"
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
            ["ssh", login_host, remote_cmd],
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
        login_host = self._login_host()
        base = self._resolve_slurm_mailbox_base()

        inbox_path = (
            f"{base}/mailbox/inbox/"
            f"{job_id}_{int(node_id):02d}.jsonl"
        )

        payload = {
            "type": "control",
            "payload": message
        }

        json_line = json.dumps(payload)
        remote_cmd = f"printf '%s\\n' {shlex.quote(json_line)} >> {inbox_path}"

        result = subprocess.run(
            ["ssh", login_host, remote_cmd],
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip())
