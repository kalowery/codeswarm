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
ROUTER_PORT = int(os.environ.get("CODESWARM_TEST_ROUTER_PORT", "8877"))
PROTOCOL = "codeswarm.router.v1"
DEFAULT_REPO = os.environ.get("CODESWARM_GITHUB_SMOKE_REPO", "AMDResearch/codeswarm-orchestrated-smoke").strip()
ROUTER_PID_FILE = ROOT / f"router-test-{ROUTER_PORT}.pid"
ROUTER_STATE_FILE = ROOT / f"router-state-test-{ROUTER_PORT}.json"


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


def wait_until(label: str, predicate, timeout: float = 120.0, interval: float = 0.2):
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

    def wait_for_event(self, predicate, timeout: float = 120.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.pump()
            for idx, event in enumerate(self.events):
                if predicate(event):
                    return self.events.pop(idx)
            time.sleep(0.1)
        raise RuntimeError("Timed out waiting for router event")


def load_router_state() -> dict:
    if not ROUTER_STATE_FILE.exists():
        return {}
    return json.loads(ROUTER_STATE_FILE.read_text(encoding="utf-8"))


def wait_for_router_state(predicate, label: str, timeout: float = 120.0):
    def _poll():
        state = load_router_state()
        return predicate(state)
    return wait_until(label, _poll, timeout=timeout, interval=0.2)


def start_router_if_needed():
    if port_open(ROUTER_HOST, ROUTER_PORT):
        return None
    env = os.environ.copy()
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
            "CODESWARM_ROUTER_STATE_FILE": str(ROUTER_STATE_FILE),
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


def require_beads():
    if shutil.which("bd") or shutil.which("beads"):
        return
    raise RuntimeError("bd CLI is required for the real orchestrated smoke test")


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
        "Reusable Codeswarm orchestrated real smoke test repository",
    ])
    created = run(["gh", "repo", "view", repo_name, "--json", "nameWithOwner,url,sshUrl,isPrivate,defaultBranchRef,viewerPermission"])
    return json.loads(created.stdout)


def clone_repo(clone_source: str) -> Path:
    base = Path(tempfile.mkdtemp(prefix="codeswarm-gh-real-smoke-clone-"))
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


def parse_task_result_block(text: str) -> dict:
    parsed: dict[str, object] = {}
    current_key: str | None = None
    start = False
    for raw_line in str(text or "").splitlines():
        line = raw_line.rstrip()
        if line.strip() == "TASK_RESULT":
            start = True
            continue
        if not start or not line.strip():
            continue
        if line.lstrip().startswith("- ") and current_key:
            parsed.setdefault(current_key, [])
            parsed[current_key].append(line.lstrip()[2:].strip())
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        current_key = key.strip().lower().replace("-", "_")
        value = value.strip()
        if current_key in ("files_changed", "verification"):
            parsed[current_key] = [value] if value else []
        else:
            parsed[current_key] = value
    return parsed


def checkout_branch_and_read(repo_dir: Path, branch: str, rel_path: str) -> str:
    run(["git", "fetch", "origin", branch], cwd=repo_dir)
    run(["git", "checkout", "-B", branch, f"origin/{branch}"], cwd=repo_dir)
    target = repo_dir / rel_path
    if not target.exists():
        raise RuntimeError(f"Expected file missing on branch {branch}: {rel_path}")
    return target.read_text(encoding="utf-8")


def main():
    require_beads()
    try:
        ROUTER_PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass
    try:
        ROUTER_STATE_FILE.unlink(missing_ok=True)
    except Exception:
        pass
    beads_home = Path(tempfile.mkdtemp(prefix="codeswarm-beads-home-"))
    os.environ["CODESWARM_BEADS_HOME"] = str(beads_home)
    atexit.register(lambda: shutil.rmtree(beads_home, ignore_errors=True))
    atexit.register(lambda: ROUTER_STATE_FILE.unlink(missing_ok=True))
    atexit.register(lambda: ROUTER_PID_FILE.unlink(missing_ok=True))
    repo_name = DEFAULT_REPO
    repo_meta = ensure_repo(repo_name)
    clone_source = str(repo_meta.get("sshUrl") or "").strip() or f"git@github.com:{repo_name}.git"
    deleted_before = cleanup_codeswarm_branches(clone_source)

    beads_repo = clone_repo(clone_source)
    atexit.register(lambda: shutil.rmtree(beads_repo.parent, ignore_errors=True))

    started_router = start_router_if_needed()
    atexit.register(lambda: terminate_process(started_router))

    client = RouterClient()
    atexit.register(client.close)

    planner_agents = (
        "# Real planner smoke worker\n"
        "Return only the TASK_GRAPH_JSON block for planning prompts.\n"
        "Start the final answer with TASK_GRAPH_JSON on its own line.\n"
        "The JSON must have a top-level `tasks` array and must never use `nodes` or `edges`.\n"
    )
    worker_agents = (
        "# Real task smoke worker\n"
        "For orchestrated project tasks, return only the TASK_RESULT block in the final answer.\n"
        "Create or switch to the assigned branch, commit your changes, and push to origin when available.\n"
    )
    codex_provider_params = {
        "worker_mode": "codex",
        "native_auto_approve": True,
        "approval_policy": "never",
        "fresh_thread_per_injection": True,
    }

    planner_request = client.send("swarm_launch", {
        "nodes": 1,
        "system_prompt": "You are a planning worker. Output only a valid TASK_GRAPH_JSON block that follows the requested schema exactly.",
        "provider_params": dict(codex_provider_params),
        "agents_md_content": planner_agents,
    })
    worker_request = client.send("swarm_launch", {
        "nodes": 1,
        "system_prompt": "You are a task worker. Follow repository task prompts exactly and output only a TASK_RESULT block.",
        "provider_params": dict(codex_provider_params),
        "agents_md_content": worker_agents,
    })

    swarms = wait_for_router_state(
        lambda state: (state.get("swarms") or {}) if len(state.get("swarms") or {}) >= 2 else None,
        "planner and worker swarms",
        timeout=120,
    )
    planner_swarm = None
    worker_swarm = None
    for swarm_id, swarm in swarms.items():
        if not isinstance(swarm, dict):
            continue
        prompt = str(swarm.get("system_prompt") or "")
        if prompt == "You are a planning worker. Output only a valid TASK_GRAPH_JSON block that follows the requested schema exactly.":
            planner_swarm = {"swarm_id": swarm_id, **swarm}
        elif prompt == "You are a task worker. Follow repository task prompts exactly and output only a TASK_RESULT block.":
            worker_swarm = {"swarm_id": swarm_id, **swarm}
    if not planner_swarm or not worker_swarm:
        raise RuntimeError("Unable to resolve launched planner/worker swarms from router state")

    project_title = f"GitHub Real Smoke {int(time.time())}"
    spec = (
        "Create exactly two implementation-ready tasks for this repository.\n"
        "Use task IDs `T-001` and `T-002`.\n"
        "Task 1 must create a file `smoke/alpha.txt` containing exactly `alpha smoke`.\n"
        "Task 2 must create a file `smoke/bravo.txt` containing exactly `bravo smoke`.\n"
        "The tasks must be independently implementable from the base branch, but Task 2 should depend_on Task 1 for scheduling order.\n"
        "Do not add extra tasks.\n"
        "Each task prompt must explicitly tell the worker the exact file path and exact file content to create.\n"
    )

    plan_request = client.send("project_plan", {
        "title": project_title,
        "repo_path": str(beads_repo),
        "spec": spec,
        "planner_swarm_id": planner_swarm["swarm_id"],
        "worker_swarm_ids": [worker_swarm["swarm_id"]],
        "base_branch": "main",
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
            status = str(plan.get("status") or "").strip().lower()
            if status == "failed":
                raise RuntimeError(f"Planner failed to produce a valid task graph: {plan.get('last_error')}")
        return None

    created_project_id = wait_for_router_state(wait_for_project_creation, "project creation", timeout=300)
    if not created_project_id:
        raise RuntimeError("Project was not created from planner output")

    def wait_for_project_terminal(state: dict):
        project = (state.get("projects") or {}).get(str(created_project_id))
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
    if str(completed_snapshot.get("beads_sync_status") or "") not in ("synced", "partial"):
        raise RuntimeError(
            f"Unexpected beads sync status: {completed_snapshot.get('beads_sync_status')} / {completed_snapshot.get('beads_last_error')}"
        )
    if not completed_snapshot.get("beads_root_id"):
        raise RuntimeError("Expected beads_root_id to be populated")

    tasks = completed_snapshot.get("tasks") or {}
    if len(tasks) != 3:
        raise RuntimeError(f"Expected 3 tasks in project snapshot including integration, got {len(tasks)}")

    expected_contents = {
        "smoke/alpha.txt": "alpha smoke",
        "smoke/bravo.txt": "bravo smoke",
    }
    created_branches: list[str] = []
    verified_heads: dict[str, str] = {}
    remote_verify_repo = clone_repo(clone_source)
    atexit.register(lambda: shutil.rmtree(remote_verify_repo.parent, ignore_errors=True))
    implementation_tasks = []
    integration_task = None

    for task in tasks.values():
        if not isinstance(task, dict):
            continue
        if not task.get("beads_id"):
            raise RuntimeError(f"Task missing beads_id: {task.get('task_id')}")
        if str(task.get("task_kind") or "").strip().lower() == "integration":
            integration_task = task
            continue
        implementation_tasks.append(task)

    if len(implementation_tasks) != 2:
        raise RuntimeError(f"Expected 2 implementation tasks, got {len(implementation_tasks)}")
    if not isinstance(integration_task, dict):
        raise RuntimeError("Expected a system-generated integration task in project snapshot")

    for task in implementation_tasks:
        parsed_result = parse_task_result_block(str(task.get("result_raw") or ""))
        branch = str(parsed_result.get("branch") or task.get("branch") or "").strip()
        if not branch:
            raise RuntimeError(f"Task missing branch in result: {task.get('task_id')}")
        created_branches.append(branch)
        head_commit = str(parsed_result.get("head_commit") or "").strip()
        remote_head = remote_branch_head(repo_name, branch)
        if not remote_head:
            raise RuntimeError(f"Remote branch missing: {branch}")
        if head_commit and remote_head != head_commit:
            raise RuntimeError(f"Remote branch {branch} head {remote_head} did not match task result {head_commit}")
        files_changed = parsed_result.get("files_changed") or []
        if not isinstance(files_changed, list) or not files_changed:
            raise RuntimeError(f"Task result missing files_changed: {task.get('task_id')}")
        matched_expected = None
        for rel_path in files_changed:
            rel_text = str(rel_path).strip()
            if rel_text in expected_contents:
                matched_expected = rel_text
                break
        if not matched_expected:
            raise RuntimeError(f"Task did not report an expected smoke file: {files_changed}")
        content = checkout_branch_and_read(remote_verify_repo, branch, matched_expected).strip()
        if content != expected_contents[matched_expected]:
            raise RuntimeError(
                f"Unexpected file content on {branch} for {matched_expected}: {content!r}"
            )
        verified_heads[branch] = remote_head

    integration_result = parse_task_result_block(str(integration_task.get("result_raw") or ""))
    integration_branch = str(
        integration_result.get("branch")
        or integration_task.get("branch")
        or completed_snapshot.get("integration_branch")
        or ""
    ).strip()
    if not integration_branch:
        raise RuntimeError("Integration task did not report an integration branch")
    integration_head = remote_branch_head(repo_name, integration_branch)
    if not integration_head:
        raise RuntimeError(f"Remote integration branch missing: {integration_branch}")
    for rel_path, expected_content in expected_contents.items():
        content = checkout_branch_and_read(remote_verify_repo, integration_branch, rel_path).strip()
        if content != expected_content:
            raise RuntimeError(
                f"Unexpected integrated content on {integration_branch} for {rel_path}: {content!r}"
            )
    created_branches.append(integration_branch)
    verified_heads[integration_branch] = integration_head

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
        "task_counts": counts,
        "beads_sync_status": completed_snapshot.get("beads_sync_status"),
        "beads_root_id": completed_snapshot.get("beads_root_id"),
        "created_branches": created_branches,
        "verified_heads": verified_heads,
        "deleted_before": deleted_before,
        "deleted_after": deleted_after,
        "beads_repo": str(beads_repo),
    }, indent=2))

    client.send("swarm_terminate", {"swarm_id": planner_swarm["swarm_id"]})
    client.send("swarm_terminate", {"swarm_id": worker_swarm["swarm_id"]})


if __name__ == "__main__":
    main()
