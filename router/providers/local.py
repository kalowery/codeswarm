import subprocess
import uuid
import shutil
import json
import os
import signal
import tarfile
import sys
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
        self.jobs: Dict[str, List[dict]] = {}

        # Root directory for local runs
        self.workspace_root = Path(
            self.config.get("workspace_root", "runs")
        )

        # Archive root (optional)
        self.archive_root = self.config.get("archive_root")
        self._load_persisted_jobs()

    def _job_dir(self, job_id: str) -> Path:
        return self.workspace_root / str(job_id)

    def _job_metadata_path(self, job_id: str) -> Path:
        return self._job_dir(job_id) / ".codeswarm-job.json"

    def _read_job_metadata(self, job_id: str) -> dict | None:
        path = self._job_metadata_path(job_id)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _write_job_metadata(self, job_id: str, updates: dict) -> None:
        path = self._job_metadata_path(job_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        current = self._read_job_metadata(job_id) or {}
        current.update(updates)
        path.write_text(json.dumps(current, indent=2), encoding="utf-8")

    def _load_persisted_jobs(self) -> None:
        if not self.workspace_root.exists():
            return
        for path in sorted(self.workspace_root.glob("local_*/.codeswarm-job.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            job_id = str(data.get("job_id") or path.parent.name)
            workers = self._normalize_worker_records(data.get("workers"))
            if workers:
                self.jobs[job_id] = workers

    def _normalize_worker_records(self, raw_workers: object) -> List[dict]:
        workers: List[dict] = []
        if not isinstance(raw_workers, list):
            return workers
        for item in raw_workers:
            if not isinstance(item, dict):
                continue
            pid = item.get("pid")
            node_id = item.get("node_id")
            start_ticks = item.get("start_ticks")
            if not isinstance(pid, int) or pid <= 0:
                continue
            if not isinstance(node_id, int) or node_id < 0:
                continue
            if start_ticks is not None and (not isinstance(start_ticks, int) or start_ticks <= 0):
                continue
            workers.append({
                "pid": pid,
                "node_id": node_id,
                "start_ticks": start_ticks,
            })
        workers.sort(key=lambda item: int(item["node_id"]))
        return workers

    def _read_proc_start_ticks(self, pid: int) -> int | None:
        try:
            stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        except Exception:
            return None
        try:
            _, rest = stat_text.rsplit(")", 1)
            fields = rest.strip().split()
            return int(fields[19])
        except Exception:
            return None

    def _read_proc_cmdline(self, pid: int) -> str:
        try:
            raw = Path(f"/proc/{pid}/cmdline").read_bytes()
            return raw.replace(b"\x00", b" ").decode("utf-8", errors="ignore")
        except Exception:
            pass
        try:
            result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "command="],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                check=False,
            )
            return (result.stdout or "").strip()
        except Exception:
            return ""

    def _is_worker_alive(self, worker: dict) -> bool:
        pid = worker.get("pid")
        start_ticks = worker.get("start_ticks")
        if not isinstance(pid, int) or pid <= 0:
            return False

        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return False

        # On Linux, validate process identity via /proc start ticks to avoid PID reuse.
        if isinstance(start_ticks, int) and start_ticks > 0:
            current_ticks = self._read_proc_start_ticks(pid)
            if current_ticks is None or current_ticks != start_ticks:
                return False

        cmdline = self._read_proc_cmdline(pid)
        if cmdline:
            return "codex_worker.py" in cmdline
        # Fallback if command line is unavailable on this platform.
        return True

    def _active_workers_for_job(self, job_id: str) -> List[dict]:
        workers = self.jobs.get(job_id)
        if not workers:
            metadata = self._read_job_metadata(job_id) or {}
            workers = self._normalize_worker_records(metadata.get("workers"))
            if workers:
                self.jobs[job_id] = workers
        if not workers:
            return []
        return [worker for worker in workers if self._is_worker_alive(worker)]

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
        workers: List[dict] = []

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

            start_ticks = self._read_proc_start_ticks(int(p.pid))
            if sys.platform.startswith("linux"):
                if not isinstance(start_ticks, int) or start_ticks <= 0:
                    raise RuntimeError(f"Unable to capture worker start time for pid {p.pid}")
            else:
                # /proc is Linux-specific; persist pid only on non-Linux hosts.
                start_ticks = None

            workers.append({
                "pid": int(p.pid),
                "node_id": i,
                "start_ticks": start_ticks,
            })

        self.jobs[job_id] = workers
        self._write_job_metadata(job_id, {
            "job_id": job_id,
            "provider": "local",
            "workers": workers,
            "node_count": int(nodes),
        })
        if callable(progress_cb):
            progress_cb("ready", f"Local swarm ready: {job_id}")
        return job_id

    def terminate(self, job_id: str, terminate_params: dict | None = None) -> None:
        workers = self.jobs.get(job_id)
        if not workers:
            metadata = self._read_job_metadata(job_id) or {}
            workers = self._normalize_worker_records(metadata.get("workers"))
        for worker in workers or []:
            pid = worker.get("pid")
            if not isinstance(pid, int) or pid <= 0:
                continue
            try:
                os.kill(pid, signal.SIGTERM)
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
        return "RUNNING" if self._active_workers_for_job(job_id) else None

    def list_active_jobs(self) -> Dict[str, str]:
        states = {}
        job_ids = set(self.jobs.keys())
        if self.workspace_root.exists():
            for path in self.workspace_root.glob("local_*/.codeswarm-job.json"):
                job_ids.add(path.parent.name)
        for job_id in sorted(job_ids):
            if self._active_workers_for_job(job_id):
                states[job_id] = "RUNNING"
        return states

    def bind_swarm(self, job_id: str, swarm_id: str, swarm_record: dict) -> None:
        self._write_job_metadata(job_id, {
            "job_id": job_id,
            "swarm_id": str(swarm_id),
            "node_count": int(swarm_record.get("node_count", 0) or 0),
            "provider": str(swarm_record.get("provider") or "local"),
            "provider_backend": str(swarm_record.get("provider_backend") or "local"),
            "provider_id": swarm_record.get("provider_id"),
            "system_prompt": swarm_record.get("system_prompt"),
            "status": swarm_record.get("status") or "running",
        })

    def recover_swarms(self) -> Dict[str, dict]:
        recovered: Dict[str, dict] = {}
        if not self.workspace_root.exists():
            return recovered
        for path in sorted(self.workspace_root.glob("local_*/.codeswarm-job.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            job_id = str(data.get("job_id") or path.parent.name)
            swarm_id = data.get("swarm_id")
            if not isinstance(swarm_id, str) or not swarm_id.strip():
                continue
            if not self._active_workers_for_job(job_id):
                continue
            node_count = data.get("node_count")
            if not isinstance(node_count, int) or node_count < 1:
                node_count = len(self._normalize_worker_records(data.get("workers")))
            recovered[swarm_id] = {
                "job_id": job_id,
                "node_count": int(node_count),
                "system_prompt": data.get("system_prompt") or "",
                "status": "running",
                "provider": str(data.get("provider") or "local"),
                "provider_backend": str(data.get("provider_backend") or "local"),
                "provider_id": data.get("provider_id"),
            }
        return recovered

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
