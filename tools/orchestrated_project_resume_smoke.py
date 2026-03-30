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
PROTOCOL = "codeswarm.router.v1"
ROUTER_HOST = os.environ.get("CODESWARM_TEST_ROUTER_HOST", "127.0.0.1")
ROUTER_PORT = int(os.environ.get("CODESWARM_TEST_ROUTER_PORT", "8931"))


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


def wait_until(label: str, predicate, timeout: float = 90.0, interval: float = 0.2):
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


def make_temp_repo() -> Path:
    repo_dir = Path(tempfile.mkdtemp(prefix="codeswarm-resume-smoke-repo-"))
    run(["git", "init", "-b", "main"], cwd=repo_dir)
    run(["git", "config", "user.email", "smoke@example.test"], cwd=repo_dir)
    run(["git", "config", "user.name", "Codeswarm Resume Smoke"], cwd=repo_dir)
    (repo_dir / "README.md").write_text("# Resume Smoke Repo\n", encoding="utf-8")
    run(["git", "add", "README.md"], cwd=repo_dir)
    run(["git", "commit", "-m", "init"], cwd=repo_dir)
    return repo_dir


def write_temp_config(temp_root: Path) -> Path:
    config = json.loads((ROOT / "configs" / "local.json").read_text(encoding="utf-8"))
    cluster = dict(config.get("cluster") or {})
    cluster["workspace_root"] = str((temp_root / "runs").resolve())
    cluster["archive_root"] = str((temp_root / "archives").resolve())
    config["cluster"] = cluster
    path = temp_root / "local.resume-smoke.json"
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return path


def load_state(state_file: Path) -> dict:
    if not state_file.exists():
        return {}
    return json.loads(state_file.read_text(encoding="utf-8"))


def wait_for_state(state_file: Path, label: str, predicate, timeout: float = 120.0):
    return wait_until(label, lambda: predicate(load_state(state_file)), timeout=timeout, interval=0.2)


def start_router(config_path: Path, state_file: Path, pid_file: Path):
    env = {
        **os.environ,
        "CODESWARM_ROUTER_HOST": ROUTER_HOST,
        "CODESWARM_ROUTER_PORT": str(ROUTER_PORT),
        "CODESWARM_ROUTER_PID_FILE": str(pid_file),
        "CODESWARM_ROUTER_STATE_FILE": str(state_file),
        "CODESWARM_DISABLE_BEADS_SYNC": "1",
    }
    proc = subprocess.Popen(
        ["python3", "-u", "-m", "router.router", "--config", str(config_path), "--daemon"],
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


def main():
    temp_root = Path(tempfile.mkdtemp(prefix="codeswarm-resume-smoke-"))
    repo_dir = make_temp_repo()
    config_path = write_temp_config(temp_root)
    state_file = temp_root / "router-state.json"
    pid_file = temp_root / "router.pid"

    atexit.register(lambda: shutil.rmtree(temp_root, ignore_errors=True))
    atexit.register(lambda: shutil.rmtree(repo_dir, ignore_errors=True))

    router_proc = start_router(config_path, state_file, pid_file)
    atexit.register(lambda: terminate_process(router_proc))

    client = RouterClient()
    atexit.register(client.close)

    launch_payload = {
        "nodes": 1,
        "provider_params": {
            "worker_mode": "mock",
            "mock_delay_ms": 4000,
            "mock_push_branches": True,
        },
        "agents_md_content": "# Mock worker\n",
    }

    first_launch_request = client.send("swarm_launch", {
        **launch_payload,
        "system_prompt": "Resume smoke worker 1",
    })
    first_swarm = wait_for_state(
        state_file,
        "first worker swarm",
        lambda state: next(
            (
                {"swarm_id": swarm_id, **swarm}
                for swarm_id, swarm in (state.get("swarms") or {}).items()
                if swarm.get("system_prompt") == "Resume smoke worker 1"
            ),
            None,
        ),
        timeout=60,
    )

    tasks = [
        {
            "task_id": "T-001",
            "title": "Create alpha marker",
            "prompt": "Create a file `resume/alpha.txt` containing exactly `alpha resume`.",
            "acceptance_criteria": ["`resume/alpha.txt` exists with exact content `alpha resume`."],
            "depends_on": [],
            "owned_paths": ["resume/alpha.txt"],
        },
        {
            "task_id": "T-002",
            "title": "Create bravo marker",
            "prompt": "Create a file `resume/bravo.txt` containing exactly `bravo resume`.",
            "acceptance_criteria": ["`resume/bravo.txt` exists with exact content `bravo resume`."],
            "depends_on": ["T-001"],
            "owned_paths": ["resume/bravo.txt"],
        },
    ]
    project_title = f"Resume Smoke Project {int(time.time())}"
    client.send("project_create", {
        "title": project_title,
        "repo_path": str(repo_dir),
        "worker_swarm_ids": [first_swarm["swarm_id"]],
        "tasks": tasks,
        "base_branch": "main",
        "workspace_subdir": "repo",
        "auto_start": True,
    })

    project_snapshot = wait_for_state(
        state_file,
        "first task complete and second task assigned",
        lambda state: next(
            (
                project
                for project in (state.get("projects") or {}).values()
                if project.get("title") == project_title
                and (project.get("tasks") or {}).get("T-001", {}).get("status") == "completed"
                and (project.get("tasks") or {}).get("T-002", {}).get("status") == "assigned"
            ),
            None,
        ),
        timeout=90,
    )
    project_id = str(project_snapshot.get("project_id"))

    client.send("swarm_terminate", {
        "swarm_id": first_swarm["swarm_id"],
        "terminate_params": {"force": True},
    })
    wait_for_state(
        state_file,
        "first worker swarm removal",
        lambda state: first_swarm["swarm_id"] not in (state.get("swarms") or {}),
        timeout=30,
    )

    second_launch_request = client.send("swarm_launch", {
        **launch_payload,
        "system_prompt": "Resume smoke worker 2",
    })
    second_swarm = wait_for_state(
        state_file,
        "second worker swarm",
        lambda state: next(
            (
                {"swarm_id": swarm_id, **swarm}
                for swarm_id, swarm in (state.get("swarms") or {}).items()
                if swarm.get("system_prompt") == "Resume smoke worker 2"
            ),
            None,
        ),
        timeout=60,
    )

    client.send("project_resume", {
        "project_id": project_id,
        "worker_swarm_ids": [second_swarm["swarm_id"]],
        "retry_failed": False,
        "reverify_completed": True,
    })

    completed_project = wait_for_state(
        state_file,
        "resumed project completion",
        lambda state: ((state.get("projects") or {}).get(project_id) if ((state.get("projects") or {}).get(project_id) or {}).get("status") == "completed" else None),
        timeout=120,
    )

    counts = completed_project.get("task_counts") or {}
    if int(counts.get("completed", 0)) != 3:
        raise RuntimeError(f"Expected 3 completed tasks including integration, got counts={counts}")
    if completed_project.get("worker_swarm_ids") != [second_swarm["swarm_id"]]:
        raise RuntimeError(f"Expected resumed worker_swarm_ids to point at replacement swarm, got {completed_project.get('worker_swarm_ids')}")
    if int(completed_project.get("resume_count") or 0) < 1:
        raise RuntimeError("Expected resume_count to be incremented")

    summary = completed_project.get("resume_summary") or {}
    if int(summary.get("kept_completed", 0)) < 1:
        raise RuntimeError(f"Expected at least one completed task to be preserved, got resume_summary={summary}")
    if int(summary.get("reset_assigned", 0)) + int(summary.get("recovered_from_branch", 0)) < 1:
        raise RuntimeError(f"Expected assigned task reconciliation during resume, got resume_summary={summary}")

    t1 = (completed_project.get("tasks") or {}).get("T-001") or {}
    t2 = (completed_project.get("tasks") or {}).get("T-002") or {}
    if t1.get("status") != "completed" or t2.get("status") != "completed":
        raise RuntimeError("Expected both implementation tasks to complete after resume")
    if int(t2.get("attempts") or 0) < 2:
        raise RuntimeError(f"Expected T-002 to be retried after resume, got attempts={t2.get('attempts')}")

    integration_branch = str(completed_project.get("integration_branch") or "").strip()
    if not integration_branch:
        raise RuntimeError("Expected integration_branch to be populated after resume")
    for rel_path, expected in (
        ("resume/alpha.txt", "alpha resume"),
        ("resume/bravo.txt", "bravo resume"),
    ):
        shown = run(["git", "show", f"{integration_branch}:{rel_path}"], cwd=repo_dir)
        if shown.stdout.strip() != expected:
            raise RuntimeError(f"Unexpected content for {rel_path} on {integration_branch}: {shown.stdout!r}")

    print(json.dumps({
        "status": "ok",
        "project_id": project_id,
        "first_launch_request": first_launch_request,
        "second_launch_request": second_launch_request,
        "first_worker_swarm_id": first_swarm["swarm_id"],
        "second_worker_swarm_id": second_swarm["swarm_id"],
        "resume_summary": summary,
        "integration_branch": integration_branch,
        "task_counts": counts,
        "t1_resume_decision": t1.get("resume_decision"),
        "t2_resume_decision": t2.get("resume_decision"),
    }, indent=2))

    client.send("swarm_terminate", {
        "swarm_id": second_swarm["swarm_id"],
        "terminate_params": {"force": True},
    })


if __name__ == "__main__":
    main()
