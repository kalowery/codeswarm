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
ROUTER_PORT = int(os.environ.get("CODESWARM_TEST_ROUTER_PORT", "8920"))
PROTOCOL = "codeswarm.router.v1"
DEFAULT_REPO = os.environ.get("CODESWARM_GITHUB_SMOKE_REPO", "AMDResearch/codeswarm-orchestrated-smoke").strip()
ROUTER_PID_FILE = ROOT / f"router-test-{ROUTER_PORT}.pid"
ROUTER_STATE_FILE = ROOT / f"router-state-test-{ROUTER_PORT}.json"


def run(cmd: list[str], cwd: Path | None = None, check: bool = True, env: dict | None = None) -> subprocess.CompletedProcess:
    completed = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=False,
        env=env,
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


def load_router_state() -> dict:
    if not ROUTER_STATE_FILE.exists():
        return {}
    return json.loads(ROUTER_STATE_FILE.read_text(encoding="utf-8"))


def wait_for_router_state(predicate, label: str, timeout: float = 180.0):
    return wait_until(label, lambda: predicate(load_router_state()), timeout=timeout, interval=0.2)


def start_router_if_needed():
    if port_open(ROUTER_HOST, ROUTER_PORT):
        return None
    env = {
        **os.environ,
        "CODESWARM_ROUTER_HOST": ROUTER_HOST,
        "CODESWARM_ROUTER_PORT": str(ROUTER_PORT),
        "CODESWARM_ROUTER_PID_FILE": str(ROUTER_PID_FILE),
        "CODESWARM_ROUTER_STATE_FILE": str(ROUTER_STATE_FILE),
        "CODESWARM_DISABLE_BEADS_SYNC": "1",
    }
    proc = subprocess.Popen(
        ["python3", "-u", "-m", "router.router", "--config", "configs/local.json", "--daemon"],
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
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
    view = run(
        ["gh", "repo", "view", repo_name, "--json", "nameWithOwner,url,sshUrl,isPrivate,defaultBranchRef,viewerPermission"],
        check=False,
    )
    if view.returncode == 0:
        return json.loads(view.stdout)
    raise RuntimeError(f"Repository does not exist and smoke test will not create it automatically: {repo_name}")


def clone_repo(clone_source: str) -> Path:
    base = Path(tempfile.mkdtemp(prefix="codeswarm-gh-empty-smoke-clone-"))
    target = base / "repo"
    run(["git", "clone", clone_source, str(target)])
    return target


def delete_remote_branch(repo_dir: Path, branch: str) -> None:
    run(["git", "push", "origin", f":{branch}"], cwd=repo_dir, check=False)


def cleanup_remote_branches(clone_source: str, branches: list[str]) -> list[str]:
    repo_dir = clone_repo(clone_source)
    try:
        deleted: list[str] = []
        seen: list[str] = []
        for branch in branches:
            branch_name = str(branch or "").strip()
            if branch_name and branch_name not in seen:
                seen.append(branch_name)
        for branch_name in reversed(seen):
            delete_remote_branch(repo_dir, branch_name)
            deleted.append(branch_name)
        return deleted
    finally:
        shutil.rmtree(repo_dir.parent, ignore_errors=True)


def checkout_branch_and_read(repo_dir: Path, branch: str, rel_path: str) -> str:
    run(["git", "fetch", "origin", branch], cwd=repo_dir)
    run(["git", "checkout", "-B", branch, f"origin/{branch}"], cwd=repo_dir)
    target = repo_dir / rel_path
    if not target.exists():
        raise RuntimeError(f"Expected file missing on branch {branch}: {rel_path}")
    return target.read_text(encoding="utf-8")


def main():
    try:
        ROUTER_PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass
    try:
        ROUTER_STATE_FILE.unlink(missing_ok=True)
    except Exception:
        pass
    atexit.register(lambda: ROUTER_PID_FILE.unlink(missing_ok=True))
    atexit.register(lambda: ROUTER_STATE_FILE.unlink(missing_ok=True))

    repo_name = DEFAULT_REPO
    repo_meta = ensure_repo(repo_name)
    clone_source = str(repo_meta.get("sshUrl") or "").strip() or f"git@github.com:{repo_name}.git"
    created_branches: list[str] = []
    cleanup_holder = {"done": False}

    def cleanup():
        if cleanup_holder["done"]:
            return
        cleanup_holder["done"] = True
        if created_branches:
            cleanup_remote_branches(clone_source, created_branches)

    atexit.register(cleanup)

    base_branch = f"codeswarm-empty-sim-{int(time.time())}"
    base_repo = clone_repo(clone_source)
    try:
        run(["git", "config", "user.email", "smoke@example.test"], cwd=base_repo)
        run(["git", "config", "user.name", "Codeswarm Smoke"], cwd=base_repo)
        run(["git", "checkout", "--orphan", base_branch], cwd=base_repo)
        run(["git", "rm", "-rf", "."], cwd=base_repo, check=False)
        run(["git", "clean", "-fdx"], cwd=base_repo, check=False)
        run(["git", "commit", "--allow-empty", "-m", f"empty base {base_branch}"], cwd=base_repo)
        run(["git", "push", "--set-upstream", "origin", base_branch], cwd=base_repo)
        created_branches.append(base_branch)
    finally:
        shutil.rmtree(base_repo.parent, ignore_errors=True)

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

    swarms = wait_for_router_state(
        lambda state: (state.get("swarms") or {}) if len(state.get("swarms") or {}) >= 2 else None,
        "planner and worker swarms",
        timeout=60,
    )
    planner_swarm = swarms.get(next((sid for sid, swarm in swarms.items() if swarm.get("system_prompt") == "You are a mock planner worker."), ""))
    worker_swarm = swarms.get(next((sid for sid, swarm in swarms.items() if swarm.get("system_prompt") == "You are a mock task worker."), ""))
    if not planner_swarm or not worker_swarm:
        raise RuntimeError(f"Unable to resolve launched swarms from router state: planner={planner_request} worker={worker_request}")
    planner_swarm_id = next(sid for sid, swarm in swarms.items() if swarm is planner_swarm)
    worker_swarm_id = next(sid for sid, swarm in swarms.items() if swarm is worker_swarm)

    graph = {
        "tasks": [
            {
                "task_id": "T-001",
                "title": "Create alpha smoke file",
                "prompt": "Create a file `empty-smoke/alpha.txt` containing exactly `alpha smoke`.",
                "acceptance_criteria": ["`empty-smoke/alpha.txt` exists with exact content `alpha smoke`."],
                "depends_on": [],
                "owned_paths": ["empty-smoke/alpha.txt"],
            },
            {
                "task_id": "T-002",
                "title": "Create bravo smoke file",
                "prompt": "Create a file `empty-smoke/bravo.txt` containing exactly `bravo smoke`.",
                "acceptance_criteria": ["`empty-smoke/bravo.txt` exists with exact content `bravo smoke`."],
                "depends_on": ["T-001"],
                "owned_paths": ["empty-smoke/bravo.txt"],
            },
        ]
    }
    project_title = f"GitHub Faux Empty {int(time.time())}"
    spec = "Create a deterministic GitHub empty-repo smoke-test task graph.\n" + f"MOCK_TASK_GRAPH_JSON: {json.dumps(graph)}"
    client.send("project_plan", {
        "title": project_title,
        "repo_mode": "github",
        "github_owner": repo_name.split("/", 1)[0],
        "github_repo": repo_name.split("/", 1)[1],
        "github_create_if_missing": False,
        "github_visibility": "private",
        "spec": spec,
        "planner_swarm_id": planner_swarm_id,
        "worker_swarm_ids": [worker_swarm_id],
        "base_branch": base_branch,
        "workspace_subdir": "repo",
        "auto_start": True,
    })

    def wait_for_project_creation(state: dict):
        for project_id, project in (state.get("projects") or {}).items():
            if isinstance(project, dict) and str(project.get("title") or "") == project_title:
                return str(project_id)
        for plan in (state.get("pending_project_plans") or {}).values():
            if not isinstance(plan, dict):
                continue
            if str(plan.get("title") or "") != project_title:
                continue
            if str(plan.get("status") or "").strip().lower() == "failed":
                raise RuntimeError(f"Planner failed: {plan.get('last_error')}")
        return None

    project_id = wait_for_router_state(wait_for_project_creation, "project creation", timeout=300)
    if not project_id:
        raise RuntimeError("Project was not created")

    def wait_for_project_terminal(state: dict):
        project = (state.get("projects") or {}).get(str(project_id))
        if isinstance(project, dict) and project.get("status") in ("completed", "error", "attention"):
            return project
        return None

    completed_snapshot = wait_for_router_state(wait_for_project_terminal, "project completion", timeout=600)
    if not completed_snapshot:
        raise RuntimeError("Project completion snapshot missing")
    if completed_snapshot.get("status") != "completed":
        raise RuntimeError(
            f"Project did not complete: status={completed_snapshot.get('status')} error={completed_snapshot.get('last_error')}"
        )

    counts = completed_snapshot.get("task_counts") or {}
    if int(counts.get("completed", 0)) != 3:
        raise RuntimeError(f"Expected 3 completed tasks including integration, got counts={counts}")
    if str(completed_snapshot.get("repo_mode") or "") != "github":
        raise RuntimeError(f"Unexpected repo_mode: {completed_snapshot.get('repo_mode')}")
    if str(completed_snapshot.get("repo_label") or "") != repo_name:
        raise RuntimeError(f"Unexpected repo_label: {completed_snapshot.get('repo_label')}")

    repo_path = str(completed_snapshot.get("repo_path") or "").strip()
    if not repo_path or repo_path == repo_name:
        raise RuntimeError(f"repo_path was not resolved to a local control clone: {repo_path!r}")

    integration_branch = str(completed_snapshot.get("integration_branch") or "").strip()
    if not integration_branch:
        raise RuntimeError("Expected integration_branch to be populated")
    created_branches.append(integration_branch)

    for task in (completed_snapshot.get("tasks") or {}).values():
        if not isinstance(task, dict):
            continue
        branch = str(task.get("branch") or "").strip()
        if branch:
            created_branches.append(branch)

    verify_repo = clone_repo(clone_source)
    try:
        expected_contents = {
            "empty-smoke/alpha.txt": "alpha smoke",
            "empty-smoke/bravo.txt": "bravo smoke",
        }
        for rel_path, expected in expected_contents.items():
            content = checkout_branch_and_read(verify_repo, integration_branch, rel_path).strip()
            if content != expected:
                raise RuntimeError(f"Unexpected file content on {integration_branch} for {rel_path}: {content!r}")
    finally:
        shutil.rmtree(verify_repo.parent, ignore_errors=True)

    deleted_branches = cleanup_remote_branches(clone_source, created_branches)
    cleanup_holder["done"] = True

    print(json.dumps({
        "status": "ok",
        "repo": repo_name,
        "project_id": project_id,
        "repo_mode": completed_snapshot.get("repo_mode"),
        "repo_label": completed_snapshot.get("repo_label"),
        "repo_path": repo_path,
        "base_branch": base_branch,
        "integration_branch": integration_branch,
        "deleted_branches_after": deleted_branches,
        "task_counts": counts,
    }, indent=2))

    client.send("swarm_terminate", {"swarm_id": planner_swarm_id})
    client.send("swarm_terminate", {"swarm_id": worker_swarm_id})


if __name__ == "__main__":
    main()
