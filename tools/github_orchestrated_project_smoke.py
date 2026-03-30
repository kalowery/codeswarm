#!/usr/bin/env python3
import atexit
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ROUTER_HOST = os.environ.get("CODESWARM_TEST_ROUTER_HOST", "127.0.0.1")
ROUTER_PORT = int(os.environ.get("CODESWARM_TEST_ROUTER_PORT", "8876"))
PROTOCOL = "codeswarm.router.v1"
DEFAULT_REPO = os.environ.get("CODESWARM_GITHUB_SMOKE_REPO", "AMDResearch/codeswarm-orchestrated-smoke").strip()
ROUTER_PID_FILE = ROOT / f"router-test-{ROUTER_PORT}.pid"


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


def wait_until(label: str, predicate, timeout: float = 60.0, interval: float = 0.2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise RuntimeError(f"Timed out waiting for {label}")


class RouterClient:
    def __init__(self):
        self.sock = socket.create_connection((ROUTER_HOST, ROUTER_PORT), timeout=5)
        self.sock.settimeout(0.2)
        self.buffer = b""
        self.events = []

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

    def wait_for_event(self, predicate, timeout: float = 60.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.pump()
            for idx, event in enumerate(self.events):
                if predicate(event):
                    return self.events.pop(idx)
            time.sleep(0.1)
        raise RuntimeError("Timed out waiting for router event")


def start_router_if_needed():
    if port_open(ROUTER_HOST, ROUTER_PORT):
        return None
    env = os.environ.copy()
    env.setdefault("CODESWARM_DISABLE_BEADS_SYNC", "1")
    proc = subprocess.Popen(
        ["python3", "-u", "-m", "router.router", "--config", "configs/local.json", "--daemon"],
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={
            **env,
            "CODESWARM_ROUTER_HOST": ROUTER_HOST,
            "CODESWARM_ROUTER_PORT": str(ROUTER_PORT),
            "CODESWARM_ROUTER_PID_FILE": str(ROUTER_PID_FILE),
        },
    )
    wait_until("router", lambda: port_open(ROUTER_HOST, ROUTER_PORT), timeout=20)
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


def ensure_repo(repo_name: str) -> dict:
    view = run(["gh", "repo", "view", repo_name, "--json", "nameWithOwner,url,sshUrl,isPrivate,defaultBranchRef,viewerPermission"], check=False)
    if view.returncode == 0:
        return json.loads(view.stdout)
    run([
        "gh",
        "repo",
        "create",
        repo_name,
        "--private",
        "--add-readme",
        "--description",
        "Reusable Codeswarm orchestrated smoke test repository",
    ])
    created = run(["gh", "repo", "view", repo_name, "--json", "nameWithOwner,url,sshUrl,isPrivate,defaultBranchRef,viewerPermission"])
    return json.loads(created.stdout)


def clone_repo(clone_source: str) -> Path:
    base = Path(tempfile.mkdtemp(prefix="codeswarm-gh-smoke-clone-"))
    target = base / "repo"
    run(["git", "clone", clone_source, str(target)])
    return target


def list_codeswarm_branches(repo_dir: Path) -> list[str]:
    result = run(["git", "ls-remote", "--heads", "origin", "codeswarm/*"], cwd=repo_dir)
    branches: list[str] = []
    for line in (result.stdout or "").splitlines():
        parts = line.strip().split()
        if len(parts) != 2:
            continue
        ref = parts[1].strip()
        prefix = "refs/heads/"
        if ref.startswith(prefix):
            branches.append(ref[len(prefix):])
    return branches


def delete_remote_branch(repo_dir: Path, branch: str) -> None:
    run(["git", "push", "origin", "--delete", branch], cwd=repo_dir)


def cleanup_codeswarm_branches(clone_source: str, branches: list[str] | None = None) -> list[str]:
    repo_dir = clone_repo(clone_source)
    try:
        to_delete = branches if branches is not None else list_codeswarm_branches(repo_dir)
        deleted: list[str] = []
        for branch in to_delete:
            delete_remote_branch(repo_dir, branch)
            deleted.append(branch)
        return deleted
    finally:
        shutil.rmtree(repo_dir.parent, ignore_errors=True)


def remote_branch_head(repo_name: str, branch: str) -> str:
    result = run(["gh", "api", f"repos/{repo_name}/git/ref/heads/{branch}"], check=False)
    if result.returncode != 0:
        encoded = branch.replace("/", "%2F")
        result = run(["gh", "api", f"repos/{repo_name}/git/ref/heads/{encoded}"])
    payload = json.loads(result.stdout)
    return str(((payload.get("object") or {}).get("sha")) or "")


def checkout_branch_and_read(repo_dir: Path, branch: str, rel_path: str) -> str:
    run(["git", "fetch", "origin", branch], cwd=repo_dir)
    run(["git", "checkout", "-B", branch, f"origin/{branch}"], cwd=repo_dir)
    target = repo_dir / rel_path
    if not target.exists():
        raise RuntimeError(f"Expected file missing on branch {branch}: {rel_path}")
    return target.read_text(encoding="utf-8")


def parse_task_result_block(text: str) -> dict:
    parsed: dict[str, object] = {}
    for line in str(text or "").splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower().replace("-", "_")
        parsed[key] = value.strip()
    return parsed


def main():
    repo_name = DEFAULT_REPO
    repo_meta = ensure_repo(repo_name)
    clone_source = str(repo_meta.get("sshUrl") or "").strip() or f"git@github.com:{repo_name}.git"
    deleted_before = cleanup_codeswarm_branches(clone_source)

    started_router = start_router_if_needed()
    atexit.register(lambda: terminate_process(started_router))

    client = RouterClient()
    atexit.register(client.close)

    launch_payload = {
        "provider_params": {"worker_mode": "mock", "mock_push_branches": True},
        "agents_md_content": "# Mock worker\n",
    }
    planner_request = client.send("swarm_launch", {
        "nodes": 1,
        "system_prompt": "You are a mock planner worker.",
        **launch_payload,
    })
    worker_request = client.send("swarm_launch", {
        "nodes": 2,
        "system_prompt": "You are a mock task worker.",
        **launch_payload,
    })

    swarms_by_request = {}
    while len(swarms_by_request) < 2:
        event = client.wait_for_event(lambda e: e.get("event") == "swarm_launched", timeout=30)
        data = event.get("data") or {}
        swarms_by_request[data.get("request_id")] = data

    planner_swarm = swarms_by_request[planner_request]
    worker_swarm = swarms_by_request[worker_request]

    graph = {
        "tasks": [
            {
                "task_id": "T-001",
                "title": "Create remote marker one",
                "prompt": "Create the first remote smoke marker file.",
                "acceptance_criteria": ["mock_tasks/T-001.txt exists on a pushed branch"],
                "depends_on": [],
                "owned_paths": ["mock_tasks/T-001.txt"],
            },
            {
                "task_id": "T-002",
                "title": "Create remote marker two",
                "prompt": "Create the second remote smoke marker file.",
                "acceptance_criteria": ["mock_tasks/T-002.txt exists on a pushed branch"],
                "depends_on": ["T-001"],
                "owned_paths": ["mock_tasks/T-002.txt"],
            },
        ]
    }
    project_title = f"GitHub Smoke Project {int(time.time())}"
    spec = "Create a deterministic GitHub smoke-test task graph.\n" + f"MOCK_TASK_GRAPH_JSON: {json.dumps(graph)}"
    client.send("project_plan", {
        "title": project_title,
        "repo_path": repo_name,
        "spec": spec,
        "planner_swarm_id": planner_swarm["swarm_id"],
        "worker_swarm_ids": [worker_swarm["swarm_id"]],
        "base_branch": "main",
        "workspace_subdir": "repo",
        "auto_start": True,
    })

    completed_snapshot = None

    def project_terminal(event):
        nonlocal completed_snapshot
        if event.get("event") != "projects_updated":
            return False
        projects = (event.get("data") or {}).get("projects") or {}
        for project in projects.values():
            if project.get("title") != project_title:
                continue
            if project.get("status") in ("completed", "error", "attention"):
                completed_snapshot = project
                return True
        return False

    client.wait_for_event(project_terminal, timeout=120)
    if not completed_snapshot:
        raise RuntimeError("Project completion snapshot missing")
    if completed_snapshot.get("status") != "completed":
        raise RuntimeError(f"Project did not complete: status={completed_snapshot.get('status')} error={completed_snapshot.get('last_error')}")

    counts = completed_snapshot.get("task_counts") or {}
    if int(counts.get("completed", 0)) != 3:
        raise RuntimeError(f"Expected 3 completed tasks including integration, got counts={counts}")

    created_branches: list[str] = []
    verified_heads: dict[str, str] = {}
    integration_branch = str(completed_snapshot.get("integration_branch") or "").strip()
    if not integration_branch:
        raise RuntimeError("Expected integration_branch to be populated")
    remote_verify_repo = clone_repo(clone_source)
    atexit.register(lambda: shutil.rmtree(remote_verify_repo.parent, ignore_errors=True))
    for task in (completed_snapshot.get("tasks") or {}).values():
        if not isinstance(task, dict):
            continue
        branch = str(task.get("branch") or "").strip()
        if not branch:
            continue
        created_branches.append(branch)
        parsed_result = parse_task_result_block(str(task.get("result_raw") or ""))
        head_commit = str(parsed_result.get("head_commit") or "").strip()
        remote_head = remote_branch_head(repo_name, branch)
        if not remote_head:
            raise RuntimeError(f"Remote branch missing: {branch}")
        if head_commit and remote_head != head_commit:
            raise RuntimeError(f"Remote branch {branch} head {remote_head} did not match task result {head_commit}")
        verified_heads[branch] = remote_head
    for rel in ("mock_tasks/T-001.txt", "mock_tasks/T-002.txt"):
        content = checkout_branch_and_read(remote_verify_repo, integration_branch, rel).strip()
        if not content:
            raise RuntimeError(f"Expected {rel} to exist on integration branch {integration_branch}")

    deleted_after = cleanup_codeswarm_branches(clone_source, created_branches)

    print(json.dumps({
        "status": "ok",
        "repo": repo_meta.get("nameWithOwner"),
        "repo_url": repo_meta.get("url"),
        "repo_ssh_url": repo_meta.get("sshUrl"),
        "viewer_permission": repo_meta.get("viewerPermission"),
        "default_branch": ((repo_meta.get("defaultBranchRef") or {}).get("name")),
        "project_id": completed_snapshot.get("project_id"),
        "planner_swarm_id": planner_swarm.get("swarm_id"),
        "worker_swarm_id": worker_swarm.get("swarm_id"),
        "integration_branch": integration_branch,
        "deleted_stale_branches_before": deleted_before,
        "verified_remote_branches": verified_heads,
        "deleted_branches_after": deleted_after,
        "task_counts": counts,
    }, indent=2))

    client.send("swarm_terminate", {"swarm_id": planner_swarm["swarm_id"]})
    client.send("swarm_terminate", {"swarm_id": worker_swarm["swarm_id"]})


if __name__ == "__main__":
    main()
