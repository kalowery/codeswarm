#!/usr/bin/env python3
import argparse
import atexit
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
import shlex
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = "codeswarm.router.v1"


def run(cmd: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    completed = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and completed.returncode != 0:
        raise RuntimeError(f"Command failed ({' '.join(cmd)}): {(completed.stderr or completed.stdout).strip()}")
    return completed


def port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def wait_until(label: str, predicate, timeout: float = 180.0, interval: float = 0.2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise RuntimeError(f"Timed out waiting for {label}")


class RouterClient:
    def __init__(self, host: str, port: int):
        self.sock = socket.create_connection((host, port), timeout=5)
        self.sock.settimeout(0.2)
        self.buffer = b""
        self.events: list[dict] = []

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass

    def send(self, command: str, payload: dict) -> str:
        request_id = str(uuid.uuid4())
        msg = {
            "protocol": PROTOCOL,
            "type": "command",
            "command": command,
            "request_id": request_id,
            "payload": payload,
        }
        self.sock.sendall((json.dumps(msg) + "\n").encode("utf-8"))
        return request_id

    def pump(self):
        while True:
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            self.buffer += chunk
            while b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                msg = json.loads(line.decode("utf-8"))
                if msg.get("type") == "event":
                    self.events.append(msg)

    def wait_for_event(self, predicate, timeout: float = 600.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.pump()
            for idx, event in enumerate(self.events):
                if predicate(event):
                    return self.events.pop(idx)
            time.sleep(0.1)
        raise RuntimeError("Timed out waiting for router event")


def write_temp_slurm_only_config(base_config_path: Path, provider_id: str) -> Path:
    config = json.loads(base_config_path.read_text(encoding="utf-8"))
    launch_providers = config.get("launch_providers") or []
    filtered = [
        provider
        for provider in launch_providers
        if isinstance(provider, dict) and str(provider.get("id") or "") == provider_id
    ]
    if not filtered:
        raise RuntimeError(f"Unable to find provider '{provider_id}' in {base_config_path}")
    cluster = dict(config.get("cluster") or {})
    slurm_cfg = cluster.get("slurm")
    if not isinstance(slurm_cfg, dict):
        raise RuntimeError("Config does not contain cluster.slurm")
    config["cluster"] = {"slurm": slurm_cfg}
    config["launch_providers"] = filtered
    tmp_root = Path(tempfile.mkdtemp(prefix="codeswarm-slurm-project-only-"))
    path = tmp_root / "slurm-only.config.json"
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return path


def start_router(config_path: Path, state_file: Path, pid_file: Path, host: str, port: int):
    env = {
        **os.environ,
        "CODESWARM_ROUTER_HOST": host,
        "CODESWARM_ROUTER_PORT": str(port),
        "CODESWARM_ROUTER_PID_FILE": str(pid_file),
        "CODESWARM_ROUTER_STATE_FILE": str(state_file),
        "CODESWARM_DISABLE_BEADS_SYNC": "1",
    }
    proc = subprocess.Popen(
        [os.environ.get("PYTHON", "python3.12"), "-u", "-m", "router.router", "--config", str(config_path), "--daemon"],
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    wait_until("router", lambda: port_open(host, port), timeout=120.0)
    return proc


def terminate_process(proc):
    if proc is None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def load_state(state_file: Path) -> dict:
    if not state_file.exists():
        return {}
    return json.loads(state_file.read_text(encoding="utf-8"))


def wait_for_project(state_file: Path, title: str, timeout: float = 3600.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = load_state(state_file)
        for project in (state.get("projects") or {}).values():
            if not isinstance(project, dict):
                continue
            if str(project.get("title") or "") != title:
                continue
            status = str(project.get("status") or "")
            if status in {"completed", "error", "attention"}:
                return project
        time.sleep(0.5)
    raise RuntimeError("Timed out waiting for project completion")


def gh_repo_view(repo_name: str) -> dict:
    completed = run(
        [
            "gh",
            "repo",
            "view",
            repo_name,
            "--json",
            "nameWithOwner,url,sshUrl,isPrivate,defaultBranchRef,viewerPermission",
        ]
    )
    return json.loads(completed.stdout)


def create_temp_repo(owner: str) -> tuple[str, dict]:
    repo_name = f"{owner}/codeswarm-slurm-claude-project-smoke-{int(time.time())}-{uuid.uuid4().hex[:8]}"
    run(
        [
            "gh",
            "repo",
            "create",
            repo_name,
            "--private",
            "--add-readme",
            "--description",
            "Temporary Codeswarm Slurm Claude orchestrated project smoke repository",
        ]
    )
    return repo_name, gh_repo_view(repo_name)


def delete_repo(repo_name: str) -> None:
    run(["gh", "repo", "delete", repo_name, "--yes"], check=False)


def clone_repo(clone_source: str) -> Path:
    base = Path(tempfile.mkdtemp(prefix="codeswarm-slurm-gh-project-clone-"))
    target = base / "repo"
    run(["git", "clone", clone_source, str(target)])
    return target


def checkout_branch_and_read(repo_dir: Path, branch: str, rel_path: str) -> str:
    run(["git", "fetch", "origin", branch], cwd=repo_dir)
    run(["git", "checkout", "-B", branch, f"origin/{branch}"], cwd=repo_dir)
    target = repo_dir / rel_path
    if not target.exists():
        raise RuntimeError(f"Expected file missing on branch {branch}: {rel_path}")
    return target.read_text(encoding="utf-8")


def ls_remote_heads(clone_source: str) -> set[str]:
    completed = run(["git", "ls-remote", "--heads", clone_source])
    heads: set[str] = set()
    for raw_line in str(completed.stdout or "").splitlines():
        parts = raw_line.strip().split()
        if len(parts) != 2:
            continue
        ref = parts[1]
        prefix = "refs/heads/"
        if ref.startswith(prefix):
            heads.add(ref[len(prefix):])
    return heads


def normalize_github_repo_ref(value: str) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.startswith("git@github.com:"):
        repo = text[len("git@github.com:"):]
        return repo[:-4] if repo.endswith(".git") else repo
    prefix = "https://github.com/"
    if text.startswith(prefix):
        repo = text[len(prefix):]
        return repo[:-4] if repo.endswith(".git") else repo.rstrip("/")
    return None


def verify_project(project: dict, repo_name: str, clone_source: str, swarm_id: str) -> dict:
    if str(project.get("status") or "") != "completed":
        raise RuntimeError(f"Project did not complete: status={project.get('status')} error={project.get('last_error')}")
    counts = project.get("task_counts") or {}
    if int(counts.get("completed", 0)) != 3:
        raise RuntimeError(f"Expected 3 completed tasks including integration, got counts={counts}")
    preparation = ((project.get("repo_preparation") or {}).get(str(swarm_id)) or {})
    origin = str(preparation.get("origin") or "").strip()
    if not origin:
        raise RuntimeError("repo_preparation did not record an origin")
    normalized_expected = normalize_github_repo_ref(clone_source)
    normalized_origin = normalize_github_repo_ref(origin)
    if normalized_expected and normalized_origin:
        if normalized_origin != normalized_expected:
            raise RuntimeError(f"Unexpected prepared origin: expected {clone_source!r}, got {origin!r}")
    elif origin != clone_source:
        raise RuntimeError(f"Unexpected prepared origin: expected {clone_source!r}, got {origin!r}")

    integration_branch = str(project.get("integration_branch") or "").strip()
    if not integration_branch:
        raise RuntimeError("Expected integration_branch to be populated")
    task_branches = [
        str(task.get("branch") or "").strip()
        for task in (project.get("tasks") or {}).values()
        if isinstance(task, dict) and str(task.get("task_kind") or "").strip().lower() != "integration"
    ]
    branches_to_verify = [branch for branch in task_branches + [integration_branch] if branch]
    remote_heads = ls_remote_heads(clone_source)
    missing = [branch for branch in branches_to_verify if branch not in remote_heads]
    if missing:
        raise RuntimeError(f"Expected remote branches are missing: {missing}")

    verify_repo = clone_repo(clone_source)
    try:
        expected_contents = {
            "runtime-project/alpha.txt": "alpha slurm project",
            "runtime-project/bravo.txt": "bravo slurm project",
        }
        for rel_path, expected in expected_contents.items():
            content = checkout_branch_and_read(verify_repo, integration_branch, rel_path).strip()
            if content != expected:
                raise RuntimeError(
                    f"Unexpected file content on {integration_branch} for {rel_path}: {content!r}"
                )
    finally:
        shutil.rmtree(verify_repo.parent, ignore_errors=True)

    return {
        "integration_branch": integration_branch,
        "task_branches": task_branches,
        "prepared_origin": origin,
    }


def init_local_repo(root: Path) -> Path:
    repo_dir = root / "local-project-repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    run(["git", "init", "-b", "main", str(repo_dir)])
    run(["git", "-C", str(repo_dir), "config", "user.name", "Codeswarm Smoke"])
    run(["git", "-C", str(repo_dir), "config", "user.email", "codeswarm-smoke@example.com"])
    (repo_dir / "README.md").write_text("# Slurm local project smoke\n", encoding="utf-8")
    run(["git", "-C", str(repo_dir), "add", "README.md"])
    run(["git", "-C", str(repo_dir), "commit", "-m", "Initial commit"])
    return repo_dir


def ssh_show_file(login_host: str, repo_path: str, branch: str, rel_path: str) -> str:
    script = (
        f"git -C {shlex.quote(repo_path)} show "
        f"{shlex.quote(f'{branch}:{rel_path}')}"
    )
    completed = run(["ssh", login_host, script])
    return str(completed.stdout or "")


def verify_local_project(project: dict, login_host: str, swarm_id: str) -> dict:
    if str(project.get("status") or "") != "completed":
        raise RuntimeError(f"Project did not complete: status={project.get('status')} error={project.get('last_error')}")
    counts = project.get("task_counts") or {}
    if int(counts.get("completed", 0)) != 3:
        raise RuntimeError(f"Expected 3 completed tasks including integration, got counts={counts}")
    preparation = ((project.get("repo_preparation") or {}).get(str(swarm_id)) or {})
    remote_source = str(preparation.get("source_path_remote") or "").strip()
    integration_branch = str(project.get("integration_branch") or "").strip()
    if not remote_source or not integration_branch:
        raise RuntimeError("Project did not record remote source path or integration branch")
    expected_contents = {
        "runtime-project/alpha.txt": "alpha slurm project",
        "runtime-project/bravo.txt": "bravo slurm project",
    }
    for rel_path, expected in expected_contents.items():
        content = ssh_show_file(login_host, remote_source, integration_branch, rel_path).strip()
        if content != expected:
            raise RuntimeError(
                f"Unexpected file content on {integration_branch} for {rel_path}: {content!r}"
            )
    return {
        "integration_branch": integration_branch,
        "remote_source": remote_source,
    }


def project_debug_summary(project: dict) -> dict:
    tasks_out: dict[str, dict] = {}
    for task_id, task in (project.get("tasks") or {}).items():
        if not isinstance(task, dict):
            continue
        tasks_out[str(task_id)] = {
            "status": task.get("status"),
            "branch": task.get("branch"),
            "last_error": task.get("last_error"),
            "last_assigned_swarm_id": task.get("last_assigned_swarm_id"),
            "last_assigned_node_id": task.get("last_assigned_node_id"),
        }
    return {
        "status": project.get("status"),
        "last_error": project.get("last_error"),
        "task_counts": project.get("task_counts"),
        "integration_branch": project.get("integration_branch"),
        "repo_preparation": project.get("repo_preparation"),
        "tasks": tasks_out,
    }


def main():
    parser = argparse.ArgumentParser(description="Run a Slurm-only Claude orchestrated project smoke against a temporary GitHub repo.")
    parser.add_argument("--config", default=str(ROOT / "configs" / "combined.json"))
    parser.add_argument("--provider", default="slurm-claude-default")
    parser.add_argument("--nodes", type=int, default=1)
    parser.add_argument("--repo-mode", choices=["github", "local"], default="github")
    parser.add_argument("--github-owner", default=os.environ.get("CODESWARM_GITHUB_OWNER", "kalowery"))
    parser.add_argument("--keep-repo", action="store_true")
    parser.add_argument("--keep-artifacts", action="store_true")
    args = parser.parse_args()

    if not str(os.environ.get("ANTHROPIC_API_KEY") or "").strip():
        raise RuntimeError("ANTHROPIC_API_KEY must be set for Slurm Claude project smoke")
    if args.repo_mode == "github":
        gh_status = run(["gh", "auth", "status"], check=False)
        if gh_status.returncode != 0:
            raise RuntimeError("gh auth is required for github repo mode")

    config_path = write_temp_slurm_only_config(Path(args.config), args.provider)
    tmp_root = config_path.parent
    state_file = tmp_root / "router_state.json"
    pid_file = tmp_root / "router.pid"
    if not args.keep_artifacts:
        atexit.register(lambda: shutil.rmtree(tmp_root, ignore_errors=True))

    repo_name = ""
    repo_meta: dict = {}
    local_repo_dir: Path | None = None
    host = "127.0.0.1"
    with socket.socket() as s:
        s.bind((host, 0))
        port = int(s.getsockname()[1])

    router_proc = None
    client = None
    swarm_id = ""
    try:
        if args.repo_mode == "github":
            repo_name, repo_meta = create_temp_repo(args.github_owner)
            clone_source = str(repo_meta.get("sshUrl") or "").strip() or f"git@github.com:{repo_name}.git"
            print(f"repo={repo_name}", flush=True)
        else:
            local_repo_dir = init_local_repo(tmp_root)
            clone_source = str(local_repo_dir.resolve())
            print(f"repo={clone_source}", flush=True)

        router_proc = start_router(config_path, state_file, pid_file, host, port)
        client = RouterClient(host, port)

        launch_request = client.send(
            "swarm_launch",
            {
                "provider": args.provider,
                "nodes": int(args.nodes),
                "system_prompt": "You are a task worker. For orchestrated project tasks, return only the TASK_RESULT block.",
                "provider_params": {
                    "worker_mode": "claude",
                    "approval_policy": "never",
                    "fresh_thread_per_injection": False,
                },
                "agents_md_content": (
                    "# Slurm Claude Project Smoke Worker\n"
                    "For orchestrated project tasks, return only the TASK_RESULT block in the final answer.\n"
                    "Create or switch to the assigned branch, commit your changes, and push to origin when available.\n"
                ),
            },
        )
        launched = client.wait_for_event(
            lambda e: e.get("event") == "swarm_launched"
            and str((e.get("data") or {}).get("request_id") or "") == launch_request,
            timeout=3600.0,
        )
        launch_data = launched.get("data") or {}
        swarm_id = str(launch_data.get("swarm_id") or "")
        print(f"swarm_id={swarm_id}", flush=True)

        project_title = f"Slurm Claude GitHub Project Smoke {int(time.time())}"
        tasks = [
            {
                "task_id": "T-001",
                "title": "Create project alpha file",
                "prompt": "Create a file `runtime-project/alpha.txt` containing exactly `alpha slurm project`.",
                "acceptance_criteria": ["`runtime-project/alpha.txt` exists with exact content `alpha slurm project`."],
                "depends_on": [],
                "owned_paths": ["runtime-project/alpha.txt"],
            },
            {
                "task_id": "T-002",
                "title": "Create project bravo file",
                "prompt": "Create a file `runtime-project/bravo.txt` containing exactly `bravo slurm project`.",
                "acceptance_criteria": ["`runtime-project/bravo.txt` exists with exact content `bravo slurm project`."],
                "depends_on": ["T-001"],
                "owned_paths": ["runtime-project/bravo.txt"],
            },
        ]
        payload = {
            "title": project_title,
            "worker_swarm_ids": [swarm_id],
            "tasks": tasks,
            "base_branch": "main",
            "workspace_subdir": "repo",
            "auto_start": True,
        }
        if args.repo_mode == "github":
            payload.update(
                {
                    "repo_mode": "github",
                    "github_owner": repo_name.split("/", 1)[0],
                    "github_repo": repo_name.split("/", 1)[1],
                    "github_create_if_missing": False,
                    "github_visibility": "private",
                }
            )
        else:
            payload.update(
                {
                    "repo_mode": "local_path",
                    "repo_path": clone_source,
                }
            )
        client.send("project_create", payload)
        project = wait_for_project(state_file, project_title, timeout=3600.0)
        print(f"project_status={project.get('status')}", flush=True)
        if str(project.get("status") or "") != "completed":
            print(json.dumps({"project_debug": project_debug_summary(project)}, indent=2), flush=True)

        if args.repo_mode == "github":
            verification = verify_project(project, repo_name, clone_source, swarm_id)
        else:
            verification = verify_local_project(project, "hpcfund", swarm_id)
        print(json.dumps({"status": "ok", "project_id": project.get("project_id"), **verification}, indent=2), flush=True)
    finally:
        if client is not None and swarm_id:
            try:
                client.send("swarm_terminate", {"swarm_id": swarm_id})
            except Exception:
                pass
            try:
                client.wait_for_event(
                    lambda e: e.get("event") == "swarm_terminated"
                    and str((e.get("data") or {}).get("swarm_id") or "") == swarm_id,
                    timeout=900.0,
                )
            except Exception:
                pass
        if client is not None:
            client.close()
        terminate_process(router_proc)
        if repo_name and not args.keep_repo:
            delete_repo(repo_name)
        if args.keep_artifacts:
            print(f"artifacts={tmp_root}", flush=True)


if __name__ == "__main__":
    main()
