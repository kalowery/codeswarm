import subprocess
import uuid
import shutil
import json
import os
import re
import signal
import tarfile
import sys
import time
import importlib.util
from pathlib import Path
from pathlib import PurePosixPath
from typing import Callable, Dict, List, Optional


from .base import ClusterProvider
from .claude_env import (
    configured_claude_env_profiles,
    resolve_claude_env_overrides,
    resolve_claude_profile_env,
)

DEFAULT_LOCAL_CONTAINER_IMAGE = str(
    os.environ.get("CODESWARM_DEFAULT_LOCAL_CONTAINER_IMAGE")
    or "ghcr.io/kalowery/codeswarm-local-worker:latest"
).strip()


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

    def _agent_dir(self, job_id: str, node_id: int) -> Path:
        return self._job_dir(job_id) / f"agent_{int(node_id):02d}"

    def _worker_heartbeat_path(self, job_id: str, node_id: int) -> Path:
        return self._agent_dir(job_id, node_id) / "heartbeat.json"

    def _worker_heartbeat_timeout_seconds(self) -> float:
        configured = self.config.get("worker_heartbeat_timeout_seconds")
        try:
            value = float(configured)
        except Exception:
            value = 30.0
        return value if value > 0 else 30.0

    def _worker_startup_grace_seconds(self) -> float:
        configured = self.config.get("worker_startup_grace_seconds")
        try:
            value = float(configured)
        except Exception:
            value = 10.0
        return value if value > 0 else 10.0

    def _has_fresh_worker_heartbeat(self, job_id: str, node_id: int) -> bool:
        path = self._worker_heartbeat_path(job_id, node_id)
        try:
            mtime = path.stat().st_mtime
        except Exception:
            return False
        return (time.time() - float(mtime)) <= self._worker_heartbeat_timeout_seconds()

    def _default_worker_sandbox_mode(self) -> str:
        configured = str(self.config.get("default_sandbox_mode") or "").strip()
        if configured:
            return configured
        if sys.platform == "darwin":
            return "danger-full-access"
        return "workspace-write"

    def _configured_claude_env_profiles(self) -> Dict[str, dict]:
        return configured_claude_env_profiles(self.config)

    def _resolve_claude_profile_env(self, launch_params: dict, base_env: dict[str, str]) -> dict[str, str]:
        return resolve_claude_profile_env(self.config, launch_params, base_env)

    def _resolve_claude_env_overrides(self, launch_params: dict, base_env: dict[str, str]) -> dict[str, str]:
        return resolve_claude_env_overrides(launch_params, base_env)

    def _execution_mode(self, launch_params: dict | None) -> str:
        params = launch_params if isinstance(launch_params, dict) else {}
        return str(params.get("execution_mode") or self.config.get("default_execution_mode") or "native").strip().lower() or "native"

    def _container_engine(self, launch_params: dict | None) -> str:
        params = launch_params if isinstance(launch_params, dict) else {}
        return str(params.get("container_engine") or self.config.get("default_container_engine") or "docker").strip().lower() or "docker"

    def _container_image(self, launch_params: dict | None) -> str:
        params = launch_params if isinstance(launch_params, dict) else {}
        explicit = str(params.get("container_image") or self.config.get("default_container_image") or "").strip()
        return explicit or DEFAULT_LOCAL_CONTAINER_IMAGE

    def _container_pull_policy(self, launch_params: dict | None) -> str:
        params = launch_params if isinstance(launch_params, dict) else {}
        return str(params.get("container_pull_policy") or self.config.get("default_container_pull_policy") or "if_not_present").strip().lower() or "if_not_present"

    def _container_cli(self, engine: str) -> str:
        binary = shutil.which(str(engine))
        if not binary:
            raise RuntimeError(f"Local container engine is unavailable on this host: {engine}")
        return binary

    def _container_image_exists(self, engine: str, image: str) -> bool:
        result = subprocess.run(
            [self._container_cli(engine), "image", "inspect", image],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        return result.returncode == 0

    def _ensure_local_container_image(self, engine: str, image: str, pull_policy: str) -> None:
        engine_bin = self._container_cli(engine)
        if image == DEFAULT_LOCAL_CONTAINER_IMAGE:
            exists = self._container_image_exists(engine, image)
            if pull_policy == "never" and not exists:
                raise RuntimeError(f"Required local container image is missing: {image}")
            if pull_policy == "always" or not exists:
                pulled = False
                pull_result = subprocess.run(
                    [engine_bin, "pull", image],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False,
                )
                if pull_result.returncode == 0:
                    pulled = True
                if pulled:
                    return
                repo_root = Path(__file__).resolve().parents[2]
                dockerfile = repo_root / "docker" / "local-worker.Dockerfile"
                if not dockerfile.exists():
                    detail = pull_result.stderr.strip() or pull_result.stdout.strip()
                    raise RuntimeError(
                        f"Missing local worker Dockerfile: {dockerfile}. "
                        f"Container pull also failed for '{image}': {detail}"
                    )
                result = subprocess.run(
                    [engine_bin, "build", "-t", image, "-f", str(dockerfile), str(repo_root)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False,
                )
                if result.returncode != 0:
                    detail = result.stderr.strip() or result.stdout.strip()
                    raise RuntimeError(f"Failed to build local worker container image '{image}': {detail}")
            return

        exists = self._container_image_exists(engine, image)
        if pull_policy == "never":
            if not exists:
                raise RuntimeError(f"Required container image is missing locally: {image}")
            return
        if pull_policy == "always" or not exists:
            result = subprocess.run(
                [engine_bin, "pull", image],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                detail = result.stderr.strip() or result.stdout.strip()
                raise RuntimeError(f"Failed to pull container image '{image}': {detail}")

    def _container_name(self, job_id: str, node_id: int) -> str:
        safe_job = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(job_id))
        return f"codeswarm-{safe_job}-{int(node_id):02d}"

    def _worker_script_name(self, worker_mode: str) -> str:
        if worker_mode == "mock":
            return "mock_worker.py"
        if worker_mode == "claude":
            return "claude_worker.py"
        return "codex_worker.py"

    def _is_container_worker(self, worker: dict) -> bool:
        return isinstance(worker.get("container_id"), str) and bool(str(worker.get("container_id")).strip())

    def _container_is_running(self, engine: str, container_id: str) -> bool:
        result = subprocess.run(
            [self._container_cli(engine), "inspect", "-f", "{{.State.Running}}", str(container_id)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return False
        return str(result.stdout or "").strip().lower() == "true"

    def _remove_container(self, engine: str, container_id: str) -> None:
        subprocess.run(
            [self._container_cli(engine), "rm", "-f", str(container_id)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )

    def _container_mounts(self) -> list[tuple[Path, str, str]]:
        repo_root = Path(__file__).resolve().parents[2].resolve()
        workspace_root = self.workspace_root.resolve()
        repo_mode = "ro"
        try:
            workspace_root.relative_to(repo_root)
            repo_mode = "rw"
        except Exception:
            pass
        mounts: list[tuple[Path, str, str]] = [
            (repo_root, str(repo_root), repo_mode),
        ]
        if workspace_root != repo_root:
            mounts.append((workspace_root, str(workspace_root), "rw"))
        home = Path.home()
        optional_mounts = [
            (home / ".codex", "/root/.codex", "ro"),
            (home / ".ssh", "/root/.ssh", "ro"),
            (home / ".gitconfig", "/root/.gitconfig", "ro"),
            (home / ".git-credentials", "/root/.git-credentials", "ro"),
        ]
        for host_path, container_path, mode in optional_mounts:
            if host_path.exists():
                mounts.append((host_path.resolve(), container_path, mode))
        return mounts

    def _container_host_path_mode(self, value: str) -> str | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            candidate = Path(text).expanduser().resolve()
        except Exception:
            return None
        best_mode: str | None = None
        best_depth = -1
        for host_path, _container_path, mode in self._container_mounts():
            try:
                resolved_host = host_path.resolve()
                candidate.relative_to(resolved_host)
            except Exception:
                continue
            depth = len(resolved_host.parts)
            if depth > best_depth:
                best_depth = depth
                best_mode = mode
        return best_mode

    def _start_container_worker(
        self,
        engine: str,
        image: str,
        job_id: str,
        node_id: int,
        agent_dir: Path,
        worker_script: Path,
        env: dict[str, str],
    ) -> str:
        engine_bin = self._container_cli(engine)
        container_name = self._container_name(job_id, node_id)
        subprocess.run(
            [engine_bin, "rm", "-f", container_name],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        cmd = [
            engine_bin,
            "run",
            "-d",
            "--name",
            container_name,
            "--workdir",
            str(agent_dir.resolve()),
        ]
        for key, value in sorted(env.items()):
            cmd.extend(["-e", f"{key}={value}"])
        for host_path, container_path, mode in self._container_mounts():
            cmd.extend(["-v", f"{host_path}:{container_path}:{mode}"])
        cmd.extend(
            [
                image,
                "python3",
                str(worker_script.resolve()),
            ]
        )
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(f"Failed to launch local container worker {node_id}: {detail}")
        container_id = str(result.stdout or "").strip()
        if not container_id:
            raise RuntimeError(f"Container engine did not return a container id for worker {node_id}")
        return container_id

    def _job_metadata_path(self, job_id: str) -> Path:
        return self._job_dir(job_id) / ".codeswarm-job.json"

    def _read_job_metadata(self, job_id: str) -> dict | None:
        path = self._job_metadata_path(job_id)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _job_is_within_startup_grace(self, job_id: str) -> bool:
        metadata = self._read_job_metadata(job_id) or {}
        launched_at = metadata.get("launched_at")
        try:
            launched_ts = float(launched_at)
        except Exception:
            return False
        return (time.time() - launched_ts) <= self._worker_startup_grace_seconds()

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
            node_id = item.get("node_id")
            if not isinstance(node_id, int) or node_id < 0:
                continue
            container_id = item.get("container_id")
            if isinstance(container_id, str) and container_id.strip():
                workers.append({
                    "container_id": container_id.strip(),
                    "container_engine": str(item.get("container_engine") or "docker").strip().lower() or "docker",
                    "container_name": str(item.get("container_name") or "").strip() or None,
                    "node_id": node_id,
                })
                continue
            pid = item.get("pid")
            start_ticks = item.get("start_ticks")
            if not isinstance(pid, int) or pid <= 0:
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

    def _is_worker_alive(self, worker: dict, job_id: str | None = None) -> bool:
        node_id = worker.get("node_id")
        if isinstance(job_id, str) and job_id and isinstance(node_id, int) and node_id >= 0:
            if self._has_fresh_worker_heartbeat(job_id, node_id):
                return True
        if self._is_container_worker(worker):
            return self._container_is_running(
                str(worker.get("container_engine") or "docker").strip().lower() or "docker",
                str(worker.get("container_id") or "").strip(),
            )

        pid = worker.get("pid")
        start_ticks = worker.get("start_ticks")

        # On non-Linux hosts, pid-only recovery is too weak because we cannot
        # reliably disambiguate pid reuse without /proc start ticks. Require a
        # fresh heartbeat for recovery in that case.
        if sys.platform != "linux" and (not isinstance(start_ticks, int) or start_ticks <= 0):
            return False

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
            return (
                ("codex_worker.py" in cmdline)
                or ("claude_worker.py" in cmdline)
                or ("mock_worker.py" in cmdline)
            )
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
        return [worker for worker in workers if self._is_worker_alive(worker, job_id=job_id)]

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

    def _write_worker_codex_config(self, agent_dir: Path, launch_params: dict | None = None) -> None:
        launch_params = launch_params if isinstance(launch_params, dict) else {}
        default_sandbox_mode = self._default_worker_sandbox_mode()
        sandbox_mode = str(
            launch_params.get("sandbox_mode") or default_sandbox_mode
        ).strip() or default_sandbox_mode
        approval_policy = str(launch_params.get("approval_policy") or "never").strip() or "never"
        network_access = launch_params.get("network_access")
        if network_access is None:
            network_access = True
        network_enabled = bool(network_access)

        lines = [
            "# Worker-local Codex overrides.",
            "# model_providers and other shared settings are inherited from ~/.codex/config.toml.",
            f'approval_policy = "{approval_policy}"',
            f'sandbox_mode = "{sandbox_mode}"',
            "",
        ]
        if sandbox_mode == "workspace-write":
            lines.extend([
                "[sandbox_workspace_write]",
                f"network_access = {str(network_enabled).lower()}",
                "",
            ])

        codex_dir = agent_dir / ".codex"
        codex_dir.mkdir(parents=True, exist_ok=True)
        (codex_dir / "config.toml").write_text("\n".join(lines), encoding="utf-8")

    @staticmethod
    def _parse_github_repo_ref(repo_path: str) -> str | None:
        text = str(repo_path or "").strip()
        if not text:
            return None
        shorthand = re.fullmatch(r"([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)", text)
        if shorthand:
            return f"{shorthand.group(1)}/{shorthand.group(2)}"
        https_match = re.fullmatch(
            r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?",
            text,
        )
        if https_match:
            return f"{https_match.group(1)}/{https_match.group(2)}"
        ssh_match = re.fullmatch(
            r"git@github\.com:([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?",
            text,
        )
        if ssh_match:
            return f"{ssh_match.group(1)}/{ssh_match.group(2)}"
        return None

    @staticmethod
    def _origin_remote_url(repo_path: Path) -> str | None:
        if not repo_path.exists() or not (repo_path / ".git").exists():
            return None
        result = subprocess.run(
            ["git", "-C", str(repo_path), "config", "--get", "remote.origin.url"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return None
        value = str(result.stdout or "").strip()
        return value or None

    @staticmethod
    def _is_local_path_like(value: str) -> bool:
        text = str(value or "").strip()
        if not text:
            return False
        if "://" in text or text.startswith("git@"):
            return False
        return Path(text).expanduser().exists() or text.startswith("/") or text.startswith("~")

    def _resolved_clone_source(self, source: str) -> tuple[str, str | None]:
        github_repo = self._parse_github_repo_ref(source)
        clone_source = str(source)
        source_path = Path(str(source)).expanduser()
        inherited_origin = self._origin_remote_url(source_path.resolve()) if source_path.exists() else None
        if github_repo:
            if shutil.which("gh"):
                ssh_url = subprocess.run(
                    ["gh", "repo", "view", github_repo, "--json", "sshUrl", "-q", ".sshUrl"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False,
                )
                if ssh_url.returncode == 0 and (ssh_url.stdout or "").strip():
                    clone_source = ssh_url.stdout.strip()
                else:
                    clone_source = f"git@github.com:{github_repo}.git"
            else:
                clone_source = f"git@github.com:{github_repo}.git"
        return clone_source, inherited_origin

    def _clone_repository(self, source: str, target: Path) -> None:
        clone_source, inherited_origin = self._resolved_clone_source(source)
        clone_cmd = ["git", "clone"]
        if self._is_local_path_like(clone_source):
            clone_cmd.append("--no-local")
        clone_cmd.extend([clone_source, str(target)])
        result = subprocess.run(
            clone_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f"clone failed for {clone_source}")
        if inherited_origin:
            subprocess.run(
                ["git", "-C", str(target), "remote", "set-url", "origin", inherited_origin],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

    @staticmethod
    def _remove_path(path: Path) -> None:
        if not path.exists():
            return
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()

    @staticmethod
    def _has_git_commits(repo_path: Path) -> bool:
        result = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", "--verify", "HEAD"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        return result.returncode == 0

    @staticmethod
    def _has_git_ref(repo_path: Path, ref_name: str) -> bool:
        result = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", "--verify", ref_name],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        return result.returncode == 0

    def _checkout_prepared_branch(self, repo_path: Path, branch_name: str, worker_id: int) -> None:
        result = subprocess.run(
            ["git", "-C", str(repo_path), "checkout", branch_name],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            return

        # Empty repositories have an unborn HEAD, so there is no branch ref to
        # check out yet. In that case create the requested branch locally.
        if not self._has_git_commits(repo_path):
            create_result = subprocess.run(
                ["git", "-C", str(repo_path), "checkout", "-B", branch_name],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            if create_result.returncode == 0:
                return
            detail = create_result.stderr.strip() or create_result.stdout.strip()
            raise RuntimeError(
                f"Failed to create branch '{branch_name}' for worker {worker_id}: {detail}"
            )

        remote_ref = f"refs/remotes/origin/{branch_name}"
        if self._has_git_ref(repo_path, remote_ref):
            track_result = subprocess.run(
                ["git", "-C", str(repo_path), "checkout", "-B", branch_name, f"origin/{branch_name}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            if track_result.returncode == 0:
                return
            detail = track_result.stderr.strip() or track_result.stdout.strip()
            raise RuntimeError(
                f"Failed to checkout remote branch '{branch_name}' for worker {worker_id}: {detail}"
            )

        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(
            f"Failed to checkout branch '{branch_name}' for worker {worker_id}: {detail}"
        )

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
        launch_params = launch_params if isinstance(launch_params, dict) else {}
        worker_mode = str(
            launch_params.get("agent_runtime")
            or launch_params.get("worker_mode")
            or "codex"
        ).strip().lower()
        execution_mode = self._execution_mode(launch_params)
        if execution_mode not in {"native", "container"}:
            raise RuntimeError(f"Unsupported local execution mode: {execution_mode}")
        container_engine = self._container_engine(launch_params)
        container_image = self._container_image(launch_params)
        container_pull_policy = self._container_pull_policy(launch_params)
        approval_policy = str(launch_params.get("approval_policy") or "never").strip().lower() or "never"
        sandbox_mode = str(
            launch_params.get("sandbox_mode") or self._default_worker_sandbox_mode()
        ).strip() or self._default_worker_sandbox_mode()
        if worker_mode not in {"codex", "claude", "mock"}:
            raise RuntimeError(f"Unsupported local agent runtime: {worker_mode}")
        if worker_mode == "claude" and execution_mode != "container":
            if importlib.util.find_spec("claude_agent_sdk") is None:
                raise RuntimeError(
                    "Claude runtime requires the Python package 'claude-agent-sdk' to be installed on the launch host"
                )
        if execution_mode == "container":
            self._ensure_local_container_image(container_engine, container_image, container_pull_policy)

        for i in range(nodes):
            agent_index = f"{i:02d}"
            agent_dir = self.workspace_root / job_id / f"agent_{agent_index}"
            agent_dir.mkdir(parents=True, exist_ok=True)
            self._apply_agents_payload(agent_dir, agents_md_content, agents_bundle)
            if worker_mode == "codex":
                self._write_worker_codex_config(agent_dir, launch_params)

            worker_name = self._worker_script_name(worker_mode)
            worker_path = Path(__file__).resolve().parents[2] / "agent" / worker_name

            env = os.environ.copy()
            env.update({
                "CODESWARM_JOB_ID": job_id,
                "CODESWARM_NODE_ID": str(i),
                "CODESWARM_BASE_DIR": str(self.workspace_root.resolve()),
                "CODESWARM_ASK_FOR_APPROVAL": approval_policy,
                "CODESWARM_SANDBOX_MODE": sandbox_mode,
            })
            if "fresh_thread_per_injection" in launch_params:
                env["CODESWARM_FRESH_THREAD_PER_INJECTION"] = (
                    "1" if bool(launch_params.get("fresh_thread_per_injection")) else "0"
                )
            if "native_auto_approve" in launch_params:
                env["CODESWARM_NATIVE_AUTO_APPROVE"] = "1" if bool(launch_params.get("native_auto_approve")) else "0"
            if worker_mode == "claude":
                env.setdefault("CLAUDE_CONFIG_DIR", str(agent_dir.resolve() / ".claude"))
                env.update(self._resolve_claude_profile_env(launch_params, env))
                env.update(self._resolve_claude_env_overrides(launch_params, env))
                claude_model = str(launch_params.get("claude_model") or "").strip()
                if claude_model:
                    env["CODESWARM_CLAUDE_MODEL"] = claude_model
                claude_cli_path = str(launch_params.get("claude_cli_path") or "").strip()
                if claude_cli_path:
                    env["CODESWARM_CLAUDE_CLI_PATH"] = claude_cli_path
                permission_mode = str(launch_params.get("claude_permission_mode") or "").strip()
                if not permission_mode:
                    permission_mode = "bypassPermissions" if approval_policy == "never" else "default"
                env["CODESWARM_CLAUDE_PERMISSION_MODE"] = permission_mode
            if worker_mode == "mock" and bool(launch_params.get("mock_push_branches")):
                env["CODESWARM_MOCK_PUSH_BRANCHES"] = "1"
            if worker_mode == "mock":
                mock_delay_ms = launch_params.get("mock_delay_ms")
                if mock_delay_ms is not None:
                    try:
                        parsed_mock_delay_ms = max(0, int(mock_delay_ms))
                    except Exception:
                        parsed_mock_delay_ms = 0
                    env["CODESWARM_MOCK_DELAY_MS"] = str(parsed_mock_delay_ms)
            if execution_mode == "container":
                container_id = self._start_container_worker(
                    container_engine,
                    container_image,
                    job_id,
                    i,
                    agent_dir,
                    worker_path,
                    env,
                )
                workers.append({
                    "container_id": container_id,
                    "container_engine": container_engine,
                    "container_name": self._container_name(job_id, i),
                    "node_id": i,
                })
            else:
                p = subprocess.Popen(
                    [sys.executable, str(worker_path)],
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
            "agent_runtime": worker_mode,
            "worker_mode": worker_mode,
            "execution_mode": execution_mode,
            "container_engine": container_engine if execution_mode == "container" else None,
            "container_image": container_image if execution_mode == "container" else None,
            "workers": workers,
            "node_count": int(nodes),
            "launched_at": time.time(),
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
            if self._is_container_worker(worker):
                self._remove_container(
                    str(worker.get("container_engine") or "docker").strip().lower() or "docker",
                    str(worker.get("container_id") or "").strip(),
                )
                continue
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

    def prepare_repository(
        self,
        job_id: str,
        repo_path: str,
        branch: str | None = None,
        subdir: str = "repo",
    ) -> dict:
        source_text = str(repo_path or "").strip()
        github_repo = self._parse_github_repo_ref(source_text)
        source_path = Path(source_text).expanduser()
        source_is_local = source_path.exists()
        metadata = self._read_job_metadata(job_id) or {}
        execution_mode = str(metadata.get("execution_mode") or "native").strip().lower() or "native"
        staged_source: Path | None = None
        if source_is_local:
            source = source_path.resolve()
            if not (source / ".git").exists():
                raise RuntimeError(f"Repository path is not a git repository: {source}")
            if execution_mode == "container":
                staged_source = (self.workspace_root / "project_sources" / job_id / "source").resolve()
                if staged_source.exists():
                    self._remove_path(staged_source)
                staged_source.parent.mkdir(parents=True, exist_ok=True)
                self._clone_repository(str(source), staged_source)
                clone_source = str(staged_source)
            else:
                clone_source = str(source)
            source_kind = "local_path"
        else:
            if not source_text:
                raise RuntimeError("Repository path is required")
            clone_source = github_repo or source_text
            source_kind = "github" if github_repo else "remote_url"

        workers = self._active_workers_for_job(job_id)
        if not workers:
            raise RuntimeError(f"No active workers found for job {job_id}")

        prepared_paths: list[str] = []
        branch_name = str(branch).strip() if isinstance(branch, str) and str(branch).strip() else None
        resolved_clone_source, inherited_origin = self._resolved_clone_source(clone_source)
        desired_origin = inherited_origin or resolved_clone_source
        if execution_mode == "container" and source_is_local and inherited_origin and self._is_local_path_like(inherited_origin):
            if self._container_host_path_mode(inherited_origin) != "rw":
                desired_origin = resolved_clone_source

        for worker in workers:
            node_id = worker.get("node_id")
            if not isinstance(node_id, int) or node_id < 0:
                continue
            agent_dir = self._agent_dir(job_id, node_id)
            agent_dir.mkdir(parents=True, exist_ok=True)
            target = (agent_dir / subdir).resolve()

            if target.exists():
                if not (target / ".git").exists():
                    self._remove_path(target)
                else:
                    current_origin = self._origin_remote_url(target)
                    if current_origin and current_origin != desired_origin:
                        self._remove_path(target)
            if not target.exists():
                try:
                    self._clone_repository(resolved_clone_source, target)
                except Exception as e:
                    raise RuntimeError(f"Failed to clone repository for worker {node_id}: {e}")
            subprocess.run(
                ["git", "-C", str(target), "remote", "set-url", "origin", desired_origin],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            fetch = subprocess.run(
                ["git", "-C", str(target), "fetch", "origin", "--prune"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            if fetch.returncode != 0:
                raise RuntimeError(
                    f"Failed to refresh repository for worker {node_id}: {fetch.stderr.strip() or fetch.stdout.strip()}"
                )

            if branch_name:
                self._checkout_prepared_branch(target, branch_name, node_id)

            prepared_paths.append(str(target))

        self._write_job_metadata(job_id, {
            "prepared_repo": {
                "source": str(source_path.resolve()) if source_is_local else clone_source,
                "source_kind": source_kind,
                "source_path_staged": str(staged_source) if staged_source else None,
                "origin": desired_origin,
                "branch": branch_name,
                "subdir": subdir,
                "worker_paths": prepared_paths,
            }
        })
        return {
            "mode": "per_agent_clone",
            "source": str(source_path.resolve()) if source_is_local else clone_source,
            "source_kind": source_kind,
            "source_path_staged": str(staged_source) if staged_source else None,
            "origin": desired_origin,
            "branch": branch_name,
            "subdir": subdir,
            "worker_paths": prepared_paths,
        }

    def get_job_state(self, job_id: str) -> Optional[str]:
        if self._active_workers_for_job(job_id):
            return "RUNNING"
        workers = self.jobs.get(job_id)
        if not workers:
            metadata = self._read_job_metadata(job_id) or {}
            workers = self._normalize_worker_records(metadata.get("workers"))
        if workers and self._job_is_within_startup_grace(job_id):
            return "STARTING"
        return None

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
            [sys.executable, str(follower_path), str(outbox_dir)],
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
