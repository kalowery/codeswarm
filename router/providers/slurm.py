import subprocess
import re
import json
import shlex
import base64
import tempfile
import time
import os
import uuid
from functools import lru_cache
from pathlib import Path
from pathlib import PurePosixPath
from typing import Callable, Dict, Optional

from .base import ClusterProvider
from .claude_env import resolve_claude_env_overrides, resolve_claude_profile_env


class SlurmProvider(ClusterProvider):

    def __init__(self, config: dict):
        self.config = config
        self.cluster_cfg = config.get("cluster", {})
        self.slurm_cfg = self.cluster_cfg.get("slurm", {})
        self._provider_ref = str(config.get("_provider_ref") or "slurm")
        self.ssh_retry_attempts = max(1, int(self.slurm_cfg.get("ssh_retry_attempts") or 4))
        self.ssh_retry_delay_seconds = max(0.2, float(self.slurm_cfg.get("ssh_retry_delay_seconds") or 1.5))

    def _login_host(self) -> str:
        slurm_login = self.slurm_cfg.get("login_host")
        if isinstance(slurm_login, str) and slurm_login.strip():
            return slurm_login.strip()
        legacy = self.slurm_cfg.get("login_alias")
        if isinstance(legacy, str) and legacy.strip():
            return legacy.strip()
        raise RuntimeError("Slurm login_host not configured in cluster.slurm")

    @staticmethod
    def _is_transient_ssh_error(stderr: str, stdout: str) -> bool:
        text = f"{stderr}\n{stdout}".lower()
        markers = [
            "connection timed out",
            "connection reset",
            "connection refused",
            "network is unreachable",
            "no route to host",
            "broken pipe",
            "kex_exchange_identification",
            "temporarily unavailable",
            "operation timed out",
            "could not resolve hostname",
            "name or service not known",
        ]
        return any(marker in text for marker in markers)

    @staticmethod
    def _runtime_mode(launch_params: dict | None) -> str:
        params = launch_params if isinstance(launch_params, dict) else {}
        return str(params.get("agent_runtime") or params.get("worker_mode") or "codex").strip().lower() or "codex"

    def _approval_policy(self, launch_params: dict | None) -> str:
        params = launch_params if isinstance(launch_params, dict) else {}
        return str(params.get("approval_policy") or self.slurm_cfg.get("approval_policy") or "never").strip().lower() or "never"

    def _claude_permission_mode(self, launch_params: dict | None) -> str:
        params = launch_params if isinstance(launch_params, dict) else {}
        explicit = str(params.get("claude_permission_mode") or self.slurm_cfg.get("claude_permission_mode") or "").strip()
        if explicit:
            return explicit
        return "bypassPermissions" if self._approval_policy(params) == "never" else "default"

    def _claude_inherited_env(self) -> dict[str, str]:
        allowed_prefixes = ("ANTHROPIC_", "CLAUDE_CODE_")
        inherited: dict[str, str] = {}
        for key, value in os.environ.items():
            if not isinstance(key, str) or value is None:
                continue
            if any(key.startswith(prefix) for prefix in allowed_prefixes):
                inherited[key] = str(value)
        return inherited

    def _resolve_claude_launch_env(self, launch_params: dict | None) -> dict[str, str]:
        params = launch_params if isinstance(launch_params, dict) else {}
        inherited_runtime_env = self._claude_inherited_env()
        expansion_env = {str(key): str(value) for key, value in os.environ.items() if value is not None}
        profile_env = resolve_claude_profile_env(self.slurm_cfg, params, expansion_env)
        inherited_runtime_env.update(profile_env)
        override_source = dict(expansion_env)
        override_source.update(inherited_runtime_env)
        inherited_runtime_env.update(resolve_claude_env_overrides(params, override_source))
        return inherited_runtime_env

    @staticmethod
    @lru_cache(maxsize=1)
    def _cached_gh_auth_token() -> str:
        completed = subprocess.run(
            ["gh", "auth", "token"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            return ""
        return str(completed.stdout or "").strip()

    def _local_github_token(self) -> str:
        for key in ("GITHUB_TOKEN", "GH_TOKEN"):
            value = str(os.environ.get(key) or "").strip()
            if value:
                return value
        return self._cached_gh_auth_token()

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

    def _resolved_clone_source(self, source: str) -> tuple[str, str | None]:
        github_repo = self._parse_github_repo_ref(source)
        clone_source = str(source)
        source_path = Path(str(source)).expanduser()
        inherited_origin = self._origin_remote_url(source_path.resolve()) if source_path.exists() else None
        if github_repo:
            clone_source = f"git@github.com:{github_repo}.git"
        return clone_source, inherited_origin

    @staticmethod
    def _github_https_url(repo_ref: str) -> str:
        github_repo = SlurmProvider._parse_github_repo_ref(repo_ref)
        if not github_repo:
            raise RuntimeError(f"Not a GitHub repository reference: {repo_ref}")
        return f"https://github.com/{github_repo}.git"

    @staticmethod
    def _github_authenticated_url(repo_ref: str, token: str) -> str:
        github_repo = SlurmProvider._parse_github_repo_ref(repo_ref)
        if not github_repo:
            raise RuntimeError(f"Not a GitHub repository reference: {repo_ref}")
        return f"https://x-access-token:{token}@github.com/{github_repo}.git"

    @staticmethod
    def _is_local_path_like(value: str) -> bool:
        text = str(value or "").strip()
        if not text:
            return False
        if "://" in text or text.startswith("git@"):
            return False
        return Path(text).expanduser().exists() or text.startswith("/") or text.startswith("~")

    @staticmethod
    def _strip_ssh_noise(text: str) -> str:
        cleaned: list[str] = []
        for raw_line in str(text or "").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("Warning: Permanently added ") and "known hosts" in line:
                continue
            cleaned.append(raw_line)
        return "\n".join(cleaned).strip()

    def _rsync_to_login_host(self, local_source: Path, remote_target: str) -> None:
        login_host = self._login_host()
        remote_parent = str(PurePosixPath(remote_target).parent)
        mkdir_res = self._ssh_run(["ssh", login_host, f"mkdir -p {shlex.quote(remote_parent)}"])
        if mkdir_res.returncode != 0:
            raise RuntimeError(f"Failed to prepare remote repository parent: {(mkdir_res.stderr or mkdir_res.stdout).strip()}")
        subprocess.run(
            [
                "rsync",
                "-az",
                "--delete",
                str(local_source.resolve()) + "/",
                f"{login_host}:{remote_target.rstrip('/')}/",
            ],
            check=True,
        )

    def _stage_github_token_file(self, token: str) -> str:
        base = self._resolve_slurm_mailbox_base()
        login_host = self._login_host()
        remote_dir = f"{base}/runtime"
        remote_path = f"{remote_dir}/github-token-{uuid.uuid4().hex}.txt"
        result = self._ssh_run(
            [
                "ssh",
                login_host,
                f"mkdir -p {shlex.quote(remote_dir)} && cat > {shlex.quote(remote_path)} && chmod 600 {shlex.quote(remote_path)}",
            ],
            input_text=str(token),
        )
        if result.returncode != 0:
            detail = (result.stderr or "").strip() or (result.stdout or "").strip()
            raise RuntimeError(f"Failed to stage GitHub token for Slurm project prep: {detail}")
        return remote_path

    def _prepared_agent_node_ids(self, job_id: str) -> list[int]:
        login_host = self._login_host()
        base = self._resolve_slurm_mailbox_base()
        script = f"""
set -euo pipefail
BASE={shlex.quote(base)}
JOB={shlex.quote(str(job_id))}
for d in "$BASE/runs/$JOB"/agent_*; do
  [ -d "$d" ] || continue
  name=$(basename "$d")
  printf '%s\n' "${{name#agent_}}"
done | sort
"""
        result = self._ssh_run(["ssh", login_host, "/bin/bash -lc " + shlex.quote(script)])
        if result.returncode != 0:
            raise RuntimeError(f"Failed to enumerate Slurm worker directories: {(result.stderr or result.stdout).strip()}")
        node_ids: list[int] = []
        for raw_line in str(result.stdout or "").splitlines():
            text = raw_line.strip()
            if not text or not text.isdigit():
                continue
            node_ids.append(int(text))
        return sorted(node_ids)

    def _checkout_prepared_branch_remote(self, repo_path: str, branch_name: str, worker_id: int) -> None:
        login_host = self._login_host()
        script = f"""
set -euo pipefail
REPO={shlex.quote(repo_path)}
BRANCH={shlex.quote(branch_name)}
if git -C "$REPO" checkout "$BRANCH"; then
  exit 0
fi
if ! git -C "$REPO" rev-parse --verify HEAD >/dev/null 2>&1; then
  git -C "$REPO" checkout -B "$BRANCH"
  exit 0
fi
if git -C "$REPO" rev-parse --verify "refs/remotes/origin/$BRANCH" >/dev/null 2>&1; then
  git -C "$REPO" checkout -B "$BRANCH" "origin/$BRANCH"
  exit 0
fi
echo "Failed to checkout branch '$BRANCH' for worker {worker_id}" >&2
exit 1
"""
        result = self._ssh_run(["ssh", login_host, "/bin/bash -lc " + shlex.quote(script)])
        combined = "\n".join(
            part
            for part in (
                self._strip_ssh_noise(result.stdout),
                self._strip_ssh_noise(result.stderr),
            )
            if part
        )
        if result.returncode != 0:
            raise RuntimeError(combined or f"Failed to checkout branch '{branch_name}'")

    def _stage_claude_env_file(self, launch_params: dict | None) -> str | None:
        env_vars = self._resolve_claude_launch_env(launch_params)
        if not env_vars:
            return None
        base = self._resolve_slurm_mailbox_base()
        login_host = self._login_host()
        remote_dir = f"{base}/runtime"
        remote_path = f"{remote_dir}/claude-env-{uuid.uuid4().hex}.sh"
        export_lines: list[str] = []
        for key in sorted(env_vars):
            value = env_vars.get(key)
            if value is None or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(key)):
                continue
            export_lines.append(f"export {key}={shlex.quote(str(value))}")
        payload = "\n".join(export_lines) + "\n"
        result = self._ssh_run(
            [
                "ssh",
                login_host,
                f"mkdir -p {shlex.quote(remote_dir)} && cat > {shlex.quote(remote_path)} && chmod 600 {shlex.quote(remote_path)}",
            ],
            input_text=payload,
        )
        if result.returncode != 0:
            detail = (result.stderr or "").strip() or (result.stdout or "").strip()
            raise RuntimeError(f"Failed to stage Claude env for Slurm launch: {detail}")
        return remote_path

    def _ssh_run(
        self,
        args: list[str],
        timeout: int | None = None,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess:
        last = None
        for attempt in range(1, self.ssh_retry_attempts + 1):
            try:
                result = subprocess.run(
                    args,
                    input=input_text,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired:
                if attempt >= self.ssh_retry_attempts:
                    raise
                time.sleep(self.ssh_retry_delay_seconds * attempt)
                continue
            last = result
            if result.returncode == 0:
                return result
            if attempt >= self.ssh_retry_attempts:
                break
            if not self._is_transient_ssh_error(result.stderr or "", result.stdout or ""):
                break
            time.sleep(self.ssh_retry_delay_seconds * attempt)
        return last

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
            runtime_config = dict(self.config)
            # allocate_and_prepare consumes a resolved single-backend config;
            # retaining launch_providers can force profile re-resolution and fail.
            runtime_config.pop("launch_providers", None)
            with tempfile.NamedTemporaryFile(
                mode="w",
                suffix=f".{self._provider_ref.replace(':', '_')}.json",
                prefix="codeswarm-slurm-",
                delete=False,
            ) as tf:
                json.dump(runtime_config, tf)
                temp_config_path = tf.name
            config_path = temp_config_path

            launch_params = launch_params if isinstance(launch_params, dict) else {}
            worker_mode = self._runtime_mode(launch_params)
            partition = launch_params.get("partition") or self.slurm_cfg.get("partition")
            time_limit = launch_params.get("time_limit") or self.slurm_cfg.get("time_limit")
            account = launch_params.get("account") if "account" in launch_params else self.slurm_cfg.get("account")
            qos = launch_params.get("qos") if "qos" in launch_params else self.slurm_cfg.get("qos")
            if isinstance(account, str) and not account.strip():
                account = None
            if isinstance(qos, str) and not qos.strip():
                qos = None
            if worker_mode not in {"codex", "claude"}:
                raise RuntimeError(f"Unsupported Slurm worker_mode: {worker_mode}")

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
                "--launch-worker-run",
                "--worker-mode",
                worker_mode,
                "--approval-policy",
                self._approval_policy(launch_params),
            ]

            if account:
                cmd += ["--account", str(account)]

            if qos:
                cmd += ["--qos", str(qos)]
            if "fresh_thread_per_injection" in launch_params:
                cmd += [
                    "--fresh-thread-per-injection",
                    "1" if bool(launch_params.get("fresh_thread_per_injection")) else "0",
                ]
            if worker_mode == "claude":
                staged_claude_env_file = self._stage_claude_env_file(launch_params)
                permission_mode = self._claude_permission_mode(launch_params)
                cmd += ["--claude-permission-mode", permission_mode]
                claude_model = str(launch_params.get("claude_model") or "").strip()
                if claude_model:
                    cmd += ["--claude-model", claude_model]
                claude_cli_path = str(launch_params.get("claude_cli_path") or "").strip()
                if claude_cli_path:
                    cmd += ["--claude-cli-path", claude_cli_path]
                if staged_claude_env_file:
                    cmd += ["--claude-env-file", staged_claude_env_file]
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
        self._ssh_run(["ssh", login_host, f"scancel {job_id}"])

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

    def prepare_repository(
        self,
        job_id: str,
        repo_path: str,
        branch: str | None = None,
        subdir: str = "repo",
    ) -> dict:
        source_text = str(repo_path or "").strip()
        if not source_text:
            raise RuntimeError("Repository path is required")

        login_host = self._login_host()
        source_path = Path(source_text).expanduser()
        source_is_local = source_path.exists()
        github_repo = self._parse_github_repo_ref(source_text)
        branch_name = str(branch).strip() if isinstance(branch, str) and str(branch).strip() else None
        source_kind = "local_path" if source_is_local else ("github" if github_repo else "remote_url")
        base = self._resolve_slurm_mailbox_base()
        remote_source = f"{base}/project_sources/{job_id}/source"
        github_token = self._local_github_token()
        github_token_file = ""
        public_origin = ""
        authenticated_origin = ""

        if source_is_local:
            source = source_path.resolve()
            if not (source / ".git").exists():
                raise RuntimeError(f"Repository path is not a git repository: {source}")
            clone_source = str(source)
            _clone_source, inherited_origin = self._resolved_clone_source(clone_source)
            desired_origin = inherited_origin if inherited_origin and not self._is_local_path_like(inherited_origin) else remote_source
            github_origin = self._parse_github_repo_ref(desired_origin)
            if github_origin:
                public_origin = self._github_https_url(github_origin)
                authenticated_origin = self._github_authenticated_url(github_origin, github_token) if github_token else public_origin
            else:
                public_origin = desired_origin
                authenticated_origin = desired_origin
            self._rsync_to_login_host(source, remote_source)
        else:
            clone_source, _inherited_origin = self._resolved_clone_source(github_repo or source_text)
            github_origin = self._parse_github_repo_ref(clone_source)
            if github_origin:
                public_origin = self._github_https_url(github_origin)
                authenticated_origin = self._github_authenticated_url(github_origin, github_token) if github_token else public_origin
            else:
                public_origin = clone_source
                authenticated_origin = clone_source
            desired_origin = public_origin
            script = f"""
set -euo pipefail
SOURCE={shlex.quote(authenticated_origin)}
TARGET={shlex.quote(remote_source)}
PARENT=$(dirname "$TARGET")
mkdir -p "$PARENT"
if [ ! -d "$TARGET/.git" ]; then
  rm -rf "$TARGET"
  git clone "$SOURCE" "$TARGET"
else
  current_origin=$(git -C "$TARGET" config --get remote.origin.url || true)
  if [ -n "$current_origin" ] && [ "$current_origin" != "$SOURCE" ]; then
    rm -rf "$TARGET"
    git clone "$SOURCE" "$TARGET"
  else
    git -C "$TARGET" remote set-url origin "$SOURCE" || true
    git -C "$TARGET" fetch origin --prune
  fi
fi
"""
            result = self._ssh_run(["ssh", login_host, "/bin/bash -lc " + shlex.quote(script)])
            if result.returncode != 0:
                raise RuntimeError(f"Failed to prepare Slurm repository source: {(result.stderr or result.stdout).strip()}")

        if github_token and (public_origin or desired_origin).startswith("https://github.com/"):
            github_token_file = self._stage_github_token_file(github_token)

        node_ids = self._prepared_agent_node_ids(str(job_id))
        if not node_ids:
            raise RuntimeError(f"No prepared worker directories found for Slurm job {job_id}")

        prepared_paths: list[str] = []
        for node_id in node_ids:
            target = f"{base}/runs/{job_id}/agent_{node_id:02d}/{subdir}"
            script_lines = [
                "set -euo pipefail",
                f"SOURCE={shlex.quote(remote_source)}",
                f"TARGET={shlex.quote(target)}",
                f"ORIGIN={shlex.quote(public_origin or desired_origin)}",
                'if [ -e "$TARGET" ] && [ ! -d "$TARGET/.git" ]; then rm -rf "$TARGET"; fi',
                'if [ -d "$TARGET/.git" ]; then',
                '  current_origin=$(git -C "$TARGET" config --get remote.origin.url || true)',
                '  if [ -n "$current_origin" ] && [ "$current_origin" != "$ORIGIN" ]; then',
                '    rm -rf "$TARGET"',
                "  fi",
                "fi",
                'if [ ! -d "$TARGET/.git" ]; then',
                '  mkdir -p "$(dirname "$TARGET")"',
                '  git clone "$SOURCE" "$TARGET"',
                "fi",
                'git -C "$TARGET" remote set-url origin "$ORIGIN" || true',
            ]
            if github_token_file:
                quoted_token_file = shlex.quote(github_token_file)
                script_lines.extend(
                    [
                        f'if printf "%s" "$ORIGIN" | grep -Eq \'^https://github\\\\.com/\'; then',
                        '  git -C "$TARGET" config credential.helper \\',
                        f"    '!f() {{ if [ \"$1\" = get ]; then echo username=x-access-token; echo password=$(cat {quoted_token_file}); fi; }}; f'",
                        '  git -C "$TARGET" config credential.useHttpPath true',
                        "else",
                        '  git -C "$TARGET" config --unset-all credential.helper || true',
                        '  git -C "$TARGET" config --unset-all credential.useHttpPath || true',
                        "fi",
                    ]
                )
            else:
                script_lines.extend(
                    [
                        'git -C "$TARGET" config --unset-all credential.helper || true',
                        'git -C "$TARGET" config --unset-all credential.useHttpPath || true',
                    ]
                )
            script_lines.append('git -C "$TARGET" fetch origin --prune || true')
            script = "\n".join(script_lines) + "\n"
            result = self._ssh_run(["ssh", login_host, "/bin/bash -lc " + shlex.quote(script)])
            if result.returncode != 0:
                raise RuntimeError(
                    f"Failed to prepare repository checkout for Slurm worker {node_id}: "
                    f"{(result.stderr or result.stdout).strip()}"
                )
            if branch_name:
                self._checkout_prepared_branch_remote(target, branch_name, node_id)
            prepared_paths.append(target)

        return {
            "mode": "per_agent_clone",
            "source": clone_source if not source_is_local else str(source_path.resolve()),
            "source_kind": source_kind,
            "source_path_remote": remote_source,
            "origin": public_origin or desired_origin,
            "branch": branch_name,
            "subdir": subdir,
            "worker_paths": prepared_paths,
        }

    def get_job_state(self, job_id: str) -> Optional[str]:
        login_host = self._login_host()

        result = self._ssh_run(
            ["ssh", login_host, f"squeue -j {job_id} -h -o '%T'"],
            timeout=15,
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
        result = self._ssh_run(cmd)

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

        result = self._ssh_run(["ssh", login_host, remote_cmd])

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

        result = self._ssh_run(["ssh", login_host, remote_cmd])

        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip())
