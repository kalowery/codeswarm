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
ROUTER_PORT = int(os.environ.get("CODESWARM_TEST_ROUTER_PORT", "8765"))
PROTOCOL = "codeswarm.router.v1"
ROUTER_PID_FILE = ROOT / f"router-test-{ROUTER_PORT}.pid"


def port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def wait_until(label: str, predicate, timeout: float = 30.0, interval: float = 0.2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise RuntimeError(f"Timed out waiting for {label}")


def make_temp_repo() -> Path:
    repo_dir = Path(tempfile.mkdtemp(prefix="codeswarm-smoke-repo-"))
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_dir, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "smoke@example.test"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "Codeswarm Smoke"], cwd=repo_dir, check=True)
    (repo_dir / "README.md").write_text("# Smoke Repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo_dir, check=True, capture_output=True, text=True)
    return repo_dir


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

    def wait_for_event(self, predicate, timeout: float = 30.0):
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
    proc = subprocess.Popen(
        ["python3", "-u", "-m", "router.router", "--config", "configs/local.json", "--daemon"],
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={
            **os.environ,
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


def main():
    started_router = start_router_if_needed()
    atexit.register(lambda: terminate_process(started_router))

    repo_dir = make_temp_repo()
    atexit.register(lambda: shutil.rmtree(repo_dir, ignore_errors=True))

    client = RouterClient()
    atexit.register(client.close)

    launch_payload = {
        "provider_params": {"worker_mode": "mock"},
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
                "title": "Create marker one",
                "prompt": "Create the first smoke marker file.",
                "acceptance_criteria": ["mock_tasks/T-001.txt exists"],
                "depends_on": [],
                "owned_paths": ["mock_tasks/T-001.txt"],
            },
            {
                "task_id": "T-002",
                "title": "Create marker two",
                "prompt": "Create the second smoke marker file.",
                "acceptance_criteria": ["mock_tasks/T-002.txt exists"],
                "depends_on": ["T-001"],
                "owned_paths": ["mock_tasks/T-002.txt"],
            },
        ]
    }
    project_title = f"Smoke Project {int(time.time())}"
    spec = "Create a deterministic smoke-test task graph.\n" + f"MOCK_TASK_GRAPH_JSON: {json.dumps(graph)}"
    client.send("project_plan", {
        "title": project_title,
        "repo_path": str(repo_dir),
        "spec": spec,
        "planner_swarm_id": planner_swarm["swarm_id"],
        "worker_swarm_ids": [worker_swarm["swarm_id"]],
        "base_branch": "main",
        "workspace_subdir": "repo",
        "auto_start": True,
    })

    completed_snapshot = None

    def project_completed(event):
        nonlocal completed_snapshot
        if event.get("event") != "projects_updated":
            return False
        projects = (event.get("data") or {}).get("projects") or {}
        for project in projects.values():
            if project.get("title") == project_title and project.get("status") == "completed":
                completed_snapshot = project
                return True
        return False

    client.wait_for_event(project_completed, timeout=60)

    if not completed_snapshot:
        raise RuntimeError("Project completion snapshot missing")
    counts = completed_snapshot.get("task_counts") or {}
    if int(counts.get("completed", 0)) != 2:
        raise RuntimeError(f"Expected 2 completed tasks, got counts={counts}")

    worker_paths = []
    for prepared in (completed_snapshot.get("repo_preparation") or {}).values():
        if isinstance(prepared, dict):
            worker_paths.extend(prepared.get("worker_paths") or [])
    if not worker_paths:
        raise RuntimeError("No prepared worker repo paths found")

    first_repo = Path(worker_paths[0])
    for rel in ("mock_tasks/T-001.txt", "mock_tasks/T-002.txt"):
        if not (first_repo / rel).exists():
            raise RuntimeError(f"Expected marker file missing: {first_repo / rel}")

    print(json.dumps({
        "status": "ok",
        "project_id": completed_snapshot.get("project_id"),
        "planner_swarm_id": planner_swarm.get("swarm_id"),
        "worker_swarm_id": worker_swarm.get("swarm_id"),
        "worker_repo": str(first_repo),
        "task_counts": counts,
    }, indent=2))

    client.send("swarm_terminate", {"swarm_id": planner_swarm["swarm_id"]})
    client.send("swarm_terminate", {"swarm_id": worker_swarm["swarm_id"]})


if __name__ == "__main__":
    main()
