#!/usr/bin/env python3
import argparse
import atexit
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = "codeswarm.router.v1"
TMP_ROOT = ROOT / ".tmp"


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

    def wait_for_event(self, predicate, timeout: float = 180.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.pump()
            for idx, event in enumerate(self.events):
                if predicate(event):
                    return self.events.pop(idx)
            time.sleep(0.1)
        raise RuntimeError("Timed out waiting for router event")


def make_repo_with_origin(prefix: str, root_dir: Path | None = None) -> tuple[Path, Path, Path]:
    if root_dir is not None:
        root_dir.mkdir(parents=True, exist_ok=True)
        base_dir = Path(tempfile.mkdtemp(prefix=prefix, dir=str(root_dir)))
    else:
        base_dir = Path(tempfile.mkdtemp(prefix=prefix))
    origin_dir = base_dir / "origin.git"
    repo_dir = base_dir / "repo"
    run(["git", "init", "--bare", str(origin_dir)])
    run(["git", "clone", "--no-local", str(origin_dir), str(repo_dir)])
    run(["git", "-C", str(repo_dir), "config", "user.email", "smoke@example.test"])
    run(["git", "-C", str(repo_dir), "config", "user.name", "Codeswarm Runtime Smoke"])
    run(["git", "-C", str(repo_dir), "checkout", "-b", "main"])
    (repo_dir / "README.md").write_text("# Runtime Smoke Repo\n", encoding="utf-8")
    run(["git", "-C", str(repo_dir), "add", "README.md"])
    run(["git", "-C", str(repo_dir), "commit", "-m", "init"])
    run(["git", "-C", str(repo_dir), "push", "-u", "origin", "main"])
    return base_dir, origin_dir, repo_dir


def write_temp_config(base_config_path: Path, temp_root: Path) -> Path:
    config = json.loads(base_config_path.read_text(encoding="utf-8"))
    cluster = dict(config.get("cluster") or {})
    if str(cluster.get("backend") or "").strip() == "local":
        cluster["workspace_root"] = str((temp_root / "runs").resolve())
        cluster["archive_root"] = str((temp_root / "archives").resolve())
    else:
        local_cfg = dict(cluster.get("local") or {})
        local_cfg["workspace_root"] = str((temp_root / "runs").resolve())
        local_cfg["archive_root"] = str((temp_root / "archives").resolve())
        cluster["local"] = local_cfg
    config["cluster"] = cluster
    path = temp_root / "runtime-smoke.config.json"
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return path


def load_state(state_file: Path) -> dict:
    if not state_file.exists():
        return {}
    return json.loads(state_file.read_text(encoding="utf-8"))


def wait_for_state(state_file: Path, label: str, predicate, timeout: float = 300.0):
    return wait_until(label, lambda: predicate(load_state(state_file)), timeout=timeout, interval=0.2)


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
        [sys.executable, "-u", "-m", "router.router", "--config", str(config_path), "--daemon"],
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    wait_until("router", lambda: port_open(host, port), timeout=20)
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


def build_provider_params(
    runtime: str,
    role: str,
    execution_mode: str = "native",
    container_engine: str = "",
    container_image: str = "",
    container_pull_policy: str = "",
) -> tuple[str, dict]:
    runtime_key = str(runtime).strip().lower()
    role_key = str(role).strip().lower()
    if role_key not in {"planner", "worker"}:
        raise RuntimeError(f"Unsupported role: {role}")

    if role_key == "planner":
        provider_id = "local-orchestrated-planner"
    elif runtime_key == "mock":
        provider_id = "local-mock-worker"
    else:
        provider_id = "local-orchestrated-worker"

    params: dict[str, object] = {
        "worker_mode": runtime_key,
        "approval_policy": "never",
        "fresh_thread_per_injection": True,
    }
    execution_mode_key = str(execution_mode or "native").strip().lower() or "native"
    params["execution_mode"] = execution_mode_key
    if execution_mode_key == "container":
        if container_engine:
            params["container_engine"] = str(container_engine).strip().lower()
        if container_image:
            params["container_image"] = str(container_image).strip()
        if container_pull_policy:
            params["container_pull_policy"] = str(container_pull_policy).strip().lower()
    if runtime_key == "codex":
        params["native_auto_approve"] = True
    if runtime_key == "mock":
        params["mock_delay_ms"] = 500
        params["mock_push_branches"] = True
    if runtime_key == "claude":
        profile = str(os.environ.get("CODESWARM_TEST_CLAUDE_ENV_PROFILE") or "").strip()
        model = str(os.environ.get("CODESWARM_TEST_CLAUDE_MODEL") or "").strip()
        if profile:
            params["claude_env_profile"] = profile
        if model:
            params["claude_model"] = model
        if not profile and not model and not str(os.environ.get("ANTHROPIC_API_KEY") or "").strip():
            raise RuntimeError(
                "Claude runtime smoke requires ANTHROPIC_API_KEY in the environment, "
                "or CODESWARM_TEST_CLAUDE_ENV_PROFILE / CODESWARM_TEST_CLAUDE_MODEL."
            )
    return provider_id, params


def planner_agents_text(runtime: str) -> str:
    runtime_key = str(runtime).strip().lower()
    if runtime_key == "mock":
        return "# Mock planner smoke worker\nReturn the deterministic MOCK_TASK_GRAPH_JSON graph.\n"
    return (
        "# Runtime planner smoke worker\n"
        "Return only the TASK_GRAPH_JSON block for planning prompts.\n"
        "Start the final answer with TASK_GRAPH_JSON on its own line.\n"
        "The JSON must have a top-level `tasks` array and must never use `nodes` or `edges`.\n"
        "Do not wrap the JSON in markdown fences.\n"
    )


def worker_agents_text(runtime: str) -> str:
    runtime_key = str(runtime).strip().lower()
    if runtime_key == "mock":
        return "# Mock task smoke worker\nFollow the task prompt deterministically and push branches when asked.\n"
    return (
        "# Runtime task smoke worker\n"
        "For orchestrated project tasks, return only the TASK_RESULT block in the final answer.\n"
        "Do not emit extra prose before or after TASK_RESULT.\n"
        "Create or switch to the assigned branch, commit your changes, and push to origin when available.\n"
    )


def parse_task_result_block(text: str) -> dict | None:
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
    return parsed or None


def remote_branch_head(repo_dir: Path, branch: str) -> str:
    result = run(["git", "ls-remote", "--heads", "origin", branch], cwd=repo_dir)
    line = next((line for line in (result.stdout or "").splitlines() if line.strip()), "")
    if not line:
        return ""
    return line.split()[0].strip()


def checkout_branch_and_read(repo_dir: Path, branch: str, rel_path: str) -> str:
    run(["git", "fetch", "origin", branch], cwd=repo_dir)
    run(["git", "checkout", "-B", branch, f"origin/{branch}"], cwd=repo_dir)
    target = repo_dir / rel_path
    if not target.exists():
        raise RuntimeError(f"Expected file missing on branch {branch}: {rel_path}")
    return target.read_text(encoding="utf-8")


def wait_for_swarm_by_request(state_file: Path, request_id: str, timeout: float = 120.0) -> dict:
    return wait_for_state(
        state_file,
        f"swarm launch for {request_id}",
        lambda state: next(
            (
                {"swarm_id": swarm_id, **swarm}
                for swarm_id, swarm in (state.get("swarms") or {}).items()
                if isinstance(swarm, dict) and str(swarm.get("request_id") or "") == request_id
            ),
            None,
        ),
        timeout=timeout,
    )


def wait_for_swarm_launch_event(client: RouterClient, request_id: str, timeout: float = 120.0) -> dict:
    event = client.wait_for_event(
        lambda e: e.get("event") == "swarm_launched" and str((e.get("data") or {}).get("request_id") or "") == request_id,
        timeout=timeout,
    )
    return dict(event.get("data") or {})


def wait_for_project_by_title(state_file: Path, title: str, predicate, timeout: float = 600.0) -> dict:
    return wait_for_state(
        state_file,
        f"project {title}",
        lambda state: next(
            (
                project
                for project in (state.get("projects") or {}).values()
                if isinstance(project, dict) and str(project.get("title") or "") == title and predicate(project)
            ),
            None,
        ),
        timeout=timeout,
    )


def verify_completed_project(completed_snapshot: dict, verify_repo: Path, expected_contents: dict[str, str]) -> dict:
    counts = completed_snapshot.get("task_counts") or {}
    expected_completed = len(expected_contents) + 1
    if int(counts.get("completed", 0)) != expected_completed:
        raise RuntimeError(
            f"Expected {expected_completed} completed tasks including integration, got counts={counts}"
        )

    tasks = completed_snapshot.get("tasks") or {}
    implementation_tasks = []
    integration_task = None
    for task in tasks.values():
        if not isinstance(task, dict):
            continue
        if str(task.get("task_kind") or "").strip().lower() == "integration":
            integration_task = task
        else:
            implementation_tasks.append(task)

    if len(implementation_tasks) != len(expected_contents):
        raise RuntimeError(
            f"Expected {len(expected_contents)} implementation tasks, got {len(implementation_tasks)}"
        )
    if not isinstance(integration_task, dict):
        raise RuntimeError("Expected an integration task in completed project snapshot")

    created_branches: list[str] = []
    verified_heads: dict[str, str] = {}
    expected_paths_remaining = dict(expected_contents)

    for task in implementation_tasks:
        parsed_result = parse_task_result_block(str(task.get("result_raw") or ""))
        branch = str((parsed_result or {}).get("branch") or task.get("branch") or "").strip()
        if not branch:
            raise RuntimeError(f"Task missing branch in result: {task.get('task_id')}")
        created_branches.append(branch)
        head_commit = str((parsed_result or {}).get("head_commit") or "").strip()
        remote_head = remote_branch_head(verify_repo, branch)
        if not remote_head:
            raise RuntimeError(f"Remote branch missing: {branch}")
        if head_commit and remote_head != head_commit:
            raise RuntimeError(f"Remote branch {branch} head {remote_head} did not match task result {head_commit}")
        files_changed = (parsed_result or {}).get("files_changed") or []
        if not isinstance(files_changed, list) or not files_changed:
            raise RuntimeError(f"Task result missing files_changed: {task.get('task_id')}")
        matched_expected = next((rel for rel in files_changed if str(rel).strip() in expected_paths_remaining), None)
        if not matched_expected:
            raise RuntimeError(f"Task did not report an expected smoke file: {files_changed}")
        content = checkout_branch_and_read(verify_repo, branch, str(matched_expected)).strip()
        if content != expected_paths_remaining[str(matched_expected)]:
            raise RuntimeError(
                f"Unexpected file content on {branch} for {matched_expected}: {content!r}"
            )
        verified_heads[branch] = remote_head
        expected_paths_remaining.pop(str(matched_expected), None)

    integration_result = parse_task_result_block(str(integration_task.get("result_raw") or ""))
    integration_branch = str(
        (integration_result or {}).get("branch")
        or integration_task.get("branch")
        or completed_snapshot.get("integration_branch")
        or ""
    ).strip()
    if not integration_branch:
        raise RuntimeError("Integration task did not report an integration branch")
    integration_head = remote_branch_head(verify_repo, integration_branch)
    if not integration_head:
        raise RuntimeError(f"Remote integration branch missing: {integration_branch}")
    for rel_path, expected_content in expected_contents.items():
        content = checkout_branch_and_read(verify_repo, integration_branch, rel_path).strip()
        if content != expected_content:
            raise RuntimeError(
                f"Unexpected integrated content on {integration_branch} for {rel_path}: {content!r}"
            )
    created_branches.append(integration_branch)
    verified_heads[integration_branch] = integration_head
    return {
        "integration_branch": integration_branch,
        "verified_heads": verified_heads,
        "created_branches": created_branches,
        "task_counts": counts,
    }


def run_direct_project(client: RouterClient, state_file: Path, worker_swarm_id: str, repo_dir: Path, verify_repo: Path, label: str) -> dict:
    project_title = f"Runtime Direct Smoke {label} {int(time.time())}"
    expected_contents = {
        "runtime-direct/alpha.txt": "alpha runtime direct",
        "runtime-direct/bravo.txt": "bravo runtime direct",
    }
    tasks = [
        {
            "task_id": "T-001",
            "title": "Create direct alpha file",
            "prompt": "Create a file `runtime-direct/alpha.txt` containing exactly `alpha runtime direct`.",
            "acceptance_criteria": ["`runtime-direct/alpha.txt` exists with exact content `alpha runtime direct`."],
            "depends_on": [],
            "owned_paths": ["runtime-direct/alpha.txt"],
        },
        {
            "task_id": "T-002",
            "title": "Create direct bravo file",
            "prompt": "Create a file `runtime-direct/bravo.txt` containing exactly `bravo runtime direct`.",
            "acceptance_criteria": ["`runtime-direct/bravo.txt` exists with exact content `bravo runtime direct`."],
            "depends_on": ["T-001"],
            "owned_paths": ["runtime-direct/bravo.txt"],
        },
    ]
    client.send("project_create", {
        "title": project_title,
        "repo_path": str(repo_dir),
        "worker_swarm_ids": [worker_swarm_id],
        "tasks": tasks,
        "base_branch": "main",
        "workspace_subdir": "repo",
        "auto_start": True,
    })
    completed = wait_for_project_by_title(
        state_file,
        project_title,
        lambda project: str(project.get("status") or "") == "completed",
        timeout=900.0,
    )
    verification = verify_completed_project(completed, verify_repo, expected_contents)
    return {
        "title": project_title,
        "project_id": completed.get("project_id"),
        **verification,
    }


def run_planned_project(
    client: RouterClient,
    state_file: Path,
    planner_swarm_id: str,
    worker_swarm_id: str,
    repo_dir: Path,
    verify_repo: Path,
    planner_runtime: str,
    label: str,
) -> dict:
    project_title = f"Runtime Planned Smoke {label} {int(time.time())}"
    expected_contents = {
        "runtime-plan/alpha.txt": "alpha runtime plan",
        "runtime-plan/bravo.txt": "bravo runtime plan",
    }
    if str(planner_runtime).strip().lower() == "mock":
        graph = {
            "tasks": [
                {
                    "task_id": "T-001",
                    "title": "Create planned alpha file",
                    "prompt": "Create a file `runtime-plan/alpha.txt` containing exactly `alpha runtime plan`.",
                    "acceptance_criteria": ["`runtime-plan/alpha.txt` exists with exact content `alpha runtime plan`."],
                    "depends_on": [],
                    "owned_paths": ["runtime-plan/alpha.txt"],
                },
                {
                    "task_id": "T-002",
                    "title": "Create planned bravo file",
                    "prompt": "Create a file `runtime-plan/bravo.txt` containing exactly `bravo runtime plan`.",
                    "acceptance_criteria": ["`runtime-plan/bravo.txt` exists with exact content `bravo runtime plan`."],
                    "depends_on": ["T-001"],
                    "owned_paths": ["runtime-plan/bravo.txt"],
                },
            ]
        }
        spec = "Create a deterministic runtime smoke task graph.\n" + f"MOCK_TASK_GRAPH_JSON: {json.dumps(graph)}"
    else:
        spec = (
            "Create exactly two implementation-ready tasks for this repository.\n"
            "Use task IDs `T-001` and `T-002`.\n"
            "Task 1 must create a file `runtime-plan/alpha.txt` containing exactly `alpha runtime plan`.\n"
            "Task 2 must create a file `runtime-plan/bravo.txt` containing exactly `bravo runtime plan`.\n"
            "Task 2 should depend_on Task 1.\n"
            "Do not add extra tasks.\n"
            "Each task prompt must explicitly tell the worker the exact file path and exact file content to create.\n"
        )
    client.send("project_plan", {
        "title": project_title,
        "repo_path": str(repo_dir),
        "spec": spec,
        "planner_swarm_id": planner_swarm_id,
        "worker_swarm_ids": [worker_swarm_id],
        "base_branch": "main",
        "workspace_subdir": "repo",
        "auto_start": True,
    })
    completed = wait_for_project_by_title(
        state_file,
        project_title,
        lambda project: str(project.get("status") or "") == "completed",
        timeout=1200.0,
    )
    verification = verify_completed_project(completed, verify_repo, expected_contents)
    return {
        "title": project_title,
        "project_id": completed.get("project_id"),
        **verification,
    }


def main():
    parser = argparse.ArgumentParser(description="Run a local orchestrated project smoke across planner/worker runtimes.")
    parser.add_argument("--planner-runtime", default="codex", choices=["codex", "claude", "mock"])
    parser.add_argument("--worker-runtime", default="codex", choices=["codex", "claude", "mock"])
    parser.add_argument("--planner-execution-mode", default="native", choices=["native", "container"])
    parser.add_argument("--worker-execution-mode", default="native", choices=["native", "container"])
    parser.add_argument("--planner-container-engine", default="")
    parser.add_argument("--worker-container-engine", default="")
    parser.add_argument("--planner-container-image", default="")
    parser.add_argument("--worker-container-image", default="")
    parser.add_argument("--planner-container-pull-policy", default="")
    parser.add_argument("--worker-container-pull-policy", default="")
    parser.add_argument("--mode", default="both", choices=["direct", "planned", "both"])
    parser.add_argument("--config", default=str(ROOT / "configs" / "local.json"))
    parser.add_argument("--router-host", default=os.environ.get("CODESWARM_TEST_ROUTER_HOST", "127.0.0.1"))
    parser.add_argument("--router-port", type=int, default=int(os.environ.get("CODESWARM_TEST_ROUTER_PORT", "8941")))
    args = parser.parse_args()

    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    temp_root = Path(tempfile.mkdtemp(prefix="codeswarm-runtime-smoke-", dir=str(TMP_ROOT)))
    config_path = write_temp_config(Path(args.config).resolve(), temp_root)
    state_file = temp_root / "router-state.json"
    pid_file = temp_root / "router.pid"
    repo_root = temp_root / "runs" / "_smoke_repos"
    atexit.register(lambda: shutil.rmtree(temp_root, ignore_errors=True))

    router_proc = start_router(config_path, state_file, pid_file, args.router_host, args.router_port)
    atexit.register(lambda: terminate_process(router_proc))

    client = RouterClient(args.router_host, args.router_port)
    atexit.register(client.close)

    planner_provider_id, planner_provider_params = build_provider_params(
        args.planner_runtime,
        "planner",
        execution_mode=args.planner_execution_mode,
        container_engine=args.planner_container_engine,
        container_image=args.planner_container_image,
        container_pull_policy=args.planner_container_pull_policy,
    )
    worker_provider_id, worker_provider_params = build_provider_params(
        args.worker_runtime,
        "worker",
        execution_mode=args.worker_execution_mode,
        container_engine=args.worker_container_engine,
        container_image=args.worker_container_image,
        container_pull_policy=args.worker_container_pull_policy,
    )

    planner_request = client.send("swarm_launch", {
        "provider_id": planner_provider_id,
        "nodes": 1,
        "system_prompt": "You are a planning worker. Output only the requested structured planning result.",
        "provider_params": planner_provider_params,
        "agents_md_content": planner_agents_text(args.planner_runtime),
    })
    worker_request = client.send("swarm_launch", {
        "provider_id": worker_provider_id,
        "nodes": 1,
        "system_prompt": "You are a task worker. Follow repository task prompts exactly and output only the requested structured result.",
        "provider_params": worker_provider_params,
        "agents_md_content": worker_agents_text(args.worker_runtime),
    })

    planner_swarm = wait_for_swarm_launch_event(client, planner_request, timeout=180.0)
    worker_swarm = wait_for_swarm_launch_event(client, worker_request, timeout=180.0)

    results: dict[str, object] = {
        "status": "ok",
        "planner_runtime": args.planner_runtime,
        "worker_runtime": args.worker_runtime,
        "planner_swarm_id": planner_swarm.get("swarm_id"),
        "worker_swarm_id": worker_swarm.get("swarm_id"),
        "planner_agent_model": planner_swarm.get("agent_model"),
        "worker_agent_model": worker_swarm.get("agent_model"),
        "planner_pricing_model": planner_swarm.get("pricing_model"),
        "worker_pricing_model": worker_swarm.get("pricing_model"),
        "modes": {},
    }

    label = f"{args.planner_runtime}-{args.worker_runtime}"
    if args.mode in ("direct", "both"):
        direct_base, _direct_origin, direct_repo = make_repo_with_origin("codeswarm-runtime-direct-", root_dir=repo_root)
        direct_verify = Path(tempfile.mkdtemp(prefix="codeswarm-runtime-direct-verify-")) / "repo"
        atexit.register(lambda path=direct_base: shutil.rmtree(path, ignore_errors=True))
        atexit.register(lambda path=direct_verify.parent: shutil.rmtree(path, ignore_errors=True))
        run(["git", "clone", "--no-local", str(direct_repo.parent / "origin.git"), str(direct_verify)])
        results["modes"]["direct"] = run_direct_project(
            client,
            state_file,
            str(worker_swarm.get("swarm_id")),
            direct_repo,
            direct_verify,
            label,
        )

    if args.mode in ("planned", "both"):
        planned_base, _planned_origin, planned_repo = make_repo_with_origin("codeswarm-runtime-planned-", root_dir=repo_root)
        planned_verify = Path(tempfile.mkdtemp(prefix="codeswarm-runtime-planned-verify-")) / "repo"
        atexit.register(lambda path=planned_base: shutil.rmtree(path, ignore_errors=True))
        atexit.register(lambda path=planned_verify.parent: shutil.rmtree(path, ignore_errors=True))
        run(["git", "clone", "--no-local", str(planned_repo.parent / "origin.git"), str(planned_verify)])
        results["modes"]["planned"] = run_planned_project(
            client,
            state_file,
            str(planner_swarm.get("swarm_id")),
            str(worker_swarm.get("swarm_id")),
            planned_repo,
            planned_verify,
            args.planner_runtime,
            label,
        )

    client.send("swarm_terminate", {"swarm_id": planner_swarm["swarm_id"]})
    client.send("swarm_terminate", {"swarm_id": worker_swarm["swarm_id"]})
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
