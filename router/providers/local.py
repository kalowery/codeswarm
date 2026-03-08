import subprocess
import uuid
import shutil
import json
import os
import tarfile
from pathlib import Path
from pathlib import PurePosixPath
from typing import Callable, Dict, List, Optional


from .base import ClusterProvider


class LocalProvider(ClusterProvider):
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

    @staticmethod
    def _safe_skill_rel_path(path: str) -> str | None:
        try:
            parts = PurePosixPath(path).parts
        except Exception:
            return None
        if not parts:
            return None
        if any(part in ("", ".", "..") for part in parts):
            return None
        return str(PurePosixPath(*parts))

    def _apply_agents_payload(self, agent_dir: Path, agents_md_content: str | None, agents_bundle: dict | None) -> None:
        bundle_mode = str((agents_bundle or {}).get("mode") or "file")
        bundle_md = (agents_bundle or {}).get("agents_md_content")
        effective_md = bundle_md if isinstance(bundle_md, str) and bundle_md.strip() else agents_md_content
        if isinstance(effective_md, str) and effective_md.strip():
            (agent_dir / "AGENTS.md").write_text(effective_md, encoding="utf-8")

        if bundle_mode != "directory":
            return

        raw_skills = (agents_bundle or {}).get("skills_files")
        if not isinstance(raw_skills, list):
            return

        for item in raw_skills:
            if not isinstance(item, dict):
                continue
            rel_path = item.get("path")
            content = item.get("content")
            if not isinstance(rel_path, str) or not isinstance(content, str):
                continue
            safe_rel = self._safe_skill_rel_path(rel_path)
            if not safe_rel:
                continue
            dest = agent_dir / ".agents" / "skills" / safe_rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")

    def launch(
        self,
        nodes: int,
        agents_md_content: str | None = None,
        agents_bundle: dict | None = None,
        launch_params: dict | None = None,
        progress_cb: Callable[[str, str], None] | None = None,
    ) -> str:
        if callable(progress_cb):
            progress_cb("starting", f"Launching {nodes} local worker(s)")
        job_id = f"local_{uuid.uuid4().hex[:8]}"
        procs: List[subprocess.Popen] = []

        for i in range(nodes):
            agent_index = f"{i:02d}"
            agent_dir = self.workspace_root / job_id / f"agent_{agent_index}"
            agent_dir.mkdir(parents=True, exist_ok=True)
            self._apply_agents_payload(agent_dir, agents_md_content, agents_bundle)

            # Locate worker relative to repository root
            worker_path = (
                Path(__file__).resolve().parents[2]
                / "agent"
                / "codex_worker.py"
            )

            env = os.environ.copy()
            env.update({
                "CODESWARM_JOB_ID": job_id,
                "CODESWARM_NODE_ID": str(i),
                "CODESWARM_BASE_DIR": str(self.workspace_root.resolve()),
            })

            p = subprocess.Popen(
                ["python3", str(worker_path)],
                cwd=str(agent_dir),
                env=env,
            )

            procs.append(p)

        self.jobs[job_id] = procs
        if callable(progress_cb):
            progress_cb("ready", f"Local swarm ready: {job_id}")
        return job_id

    def terminate(self, job_id: str, terminate_params: dict | None = None) -> None:
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

        archive_root = Path(self.archive_root)
        archive_root.mkdir(parents=True, exist_ok=True)

        target = archive_root / f"swarm_{swarm_id}_{job_id}"
        target.mkdir(parents=True, exist_ok=True)

        try:
            if runs_dir.exists():
                # Keep the archived job self-contained under a single directory.
                shutil.move(str(runs_dir), str(target / job_id))

            mailbox_root = self.workspace_root / "mailbox"
            for bucket in ("inbox", "outbox", "archive"):
                source_dir = mailbox_root / bucket
                if not source_dir.exists():
                    continue

                for path in source_dir.glob(f"{job_id}_*.jsonl"):
                    # Worker rotates completed outbox files into mailbox/archive.
                    # In archive layout, keep a single outbox bucket.
                    dest_bucket = "outbox" if bucket == "archive" else bucket
                    dest_dir = target / "mailbox" / dest_bucket
                    dest_dir.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(path), str(dest_dir / path.name))
        except Exception as e:
            print(f"[archive] LocalProvider failed to archive {swarm_id}: {e}")

    def create_workspace_archive(self, job_id: str, swarm_id: str, output_dir: Path) -> str | None:
        output_dir.mkdir(parents=True, exist_ok=True)
        archive_path = output_dir / f"swarm_{swarm_id}_{job_id}_workspaces.tar.gz"

        runs_dir = self.workspace_root / job_id
        mailbox_root = self.workspace_root / "mailbox"
        included = 0

        with tarfile.open(archive_path, "w:gz") as tar:
            if runs_dir.exists():
                tar.add(runs_dir, arcname=f"runs/{job_id}")
                included += 1

            for bucket in ("inbox", "outbox", "archive"):
                source_dir = mailbox_root / bucket
                if not source_dir.exists():
                    continue
                for path in source_dir.glob(f"{job_id}_*.jsonl"):
                    tar.add(path, arcname=f"mailbox/{bucket}/{path.name}")
                    included += 1

        if included == 0:
            try:
                archive_path.unlink(missing_ok=True)
            except Exception:
                pass
            return None

        return str(archive_path.resolve())

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

    def start_follower(self):
        follower_path = (
            Path(__file__).resolve().parents[2]
            / "agent"
            / "outbox_follower.py"
        )

        outbox_dir = self.workspace_root.resolve() / "mailbox" / "outbox"
        outbox_dir.mkdir(parents=True, exist_ok=True)

        return subprocess.Popen(
            ["python3", str(follower_path), str(outbox_dir)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def inject(self, job_id, node_id, content, injection_id):
        node_index = f"{int(node_id):02d}"

        inbox_dir = self.workspace_root.resolve() / "mailbox" / "inbox"
        inbox_dir.mkdir(parents=True, exist_ok=True)

        inbox_path = inbox_dir / f"{job_id}_{node_index}.jsonl"

        payload = {
            "type": "user",
            "content": content,
            "injection_id": injection_id
        }

        with open(inbox_path, "a") as f:
            f.write(json.dumps(payload) + "\n")

    def send_control(self, job_id: str, node_id: int, message: dict) -> None:
        """
        Send control message (e.g., exec_approval_response) to a specific worker node.
        Mirrors the inject() path so the worker reads it from the same inbox stream.
        """
        node_index = f"{int(node_id):02d}"

        inbox_dir = self.workspace_root.resolve() / "mailbox" / "inbox"
        inbox_dir.mkdir(parents=True, exist_ok=True)

        inbox_path = inbox_dir / f"{job_id}_{node_index}.jsonl"

        payload = {
            "type": "control",
            "payload": message
        }

        with open(inbox_path, "a") as f:
            f.write(json.dumps(payload) + "\n")

        # Trace approval/control routing to node inbox for debugging.
        try:
            method = message.get("method") if isinstance(message, dict) else None
            rpc_id = message.get("rpc_id") if isinstance(message, dict) else None
            params = message.get("params") if isinstance(message, dict) else None
            call_id = None
            if isinstance(params, dict):
                call_id = params.get("call_id") or params.get("callId")
            print(
                f"[local PROVIDER CONTROL] job_id={job_id} node_id={int(node_id)} method={method} rpc_id={rpc_id} call_id={call_id}",
                flush=True,
            )
        except Exception:
            pass
