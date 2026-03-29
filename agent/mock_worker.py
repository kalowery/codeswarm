#!/usr/bin/env python3
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path


def write_jsonl(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload) + "\n")


def rpc_event(job_id: str, node_id: int, injection_id: str | None, method: str, params: dict | None = None) -> dict:
    payload = {"method": method}
    if params is not None:
        payload["params"] = params
    return {
        "type": "codex_rpc",
        "job_id": job_id,
        "node_id": node_id,
        "injection_id": injection_id,
        "payload": payload,
    }


def extract_between(text: str, prefix: str) -> str:
    for line in text.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    return ""


def extract_task_graph_override(spec_text: str):
    marker = "MOCK_TASK_GRAPH_JSON:"
    idx = spec_text.find(marker)
    if idx < 0:
        return None
    raw = spec_text[idx + len(marker):].strip()
    try:
        parsed = json.loads(raw)
    except Exception:
        return None
    return parsed


def build_mock_task_graph(spec_text: str) -> dict:
    override = extract_task_graph_override(spec_text)
    if isinstance(override, dict):
        return override
    tasks = [
        {
            "task_id": "T-001",
            "title": "Create project scaffold marker",
            "prompt": "Create a mock implementation marker file for the project scaffold.",
            "acceptance_criteria": [
                "A mock marker file exists in the repo clone.",
                "The change is committed on the assigned branch."
            ],
            "depends_on": [],
            "owned_paths": ["mock_tasks/scaffold.txt"]
        },
        {
            "task_id": "T-002",
            "title": "Create feature marker",
            "prompt": "Create a second mock marker file representing a downstream feature task.",
            "acceptance_criteria": [
                "A second mock marker file exists in the repo clone.",
                "The change is committed on the assigned branch."
            ],
            "depends_on": ["T-001"],
            "owned_paths": ["mock_tasks/feature.txt"]
        }
    ]
    return {"tasks": tasks}


def build_task_graph_response(spec_text: str) -> str:
    graph = build_mock_task_graph(spec_text)
    return "TASK_GRAPH_JSON\n```json\n" + json.dumps(graph, indent=2) + "\n```"


def run_git(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, check=False)


def ensure_git_identity(repo_dir: Path) -> None:
    run_git(["git", "config", "user.email", "codeswarm-mock@example.test"], repo_dir)
    run_git(["git", "config", "user.name", "Codeswarm Mock Worker"], repo_dir)


def build_task_result(content: str, repo_dir: Path) -> str:
    task_id = extract_between(content, "You are executing orchestrated project task ").split(".")[0].strip() or "T-UNKNOWN"
    branch = extract_between(content, "Working branch to create/use: ") or f"codeswarm/mock/{task_id.lower()}"
    title = extract_between(content, "Task title: ") or task_id
    push_enabled = str(os.environ.get("CODESWARM_MOCK_PUSH_BRANCHES") or "").strip().lower() in ("1", "true", "yes", "on")
    ensure_git_identity(repo_dir)
    run_git(["git", "checkout", "-B", branch], repo_dir)
    task_dir = repo_dir / "mock_tasks"
    task_dir.mkdir(parents=True, exist_ok=True)
    filename_token = re.sub(r"[^a-zA-Z0-9._-]+", "_", task_id).strip("_") or "task"
    target = task_dir / f"{filename_token}.txt"
    target.write_text(f"{task_id}\n{title}\n", encoding="utf-8")
    run_git(["git", "add", str(target.relative_to(repo_dir))], repo_dir)
    commit = run_git(["git", "commit", "-m", f"mock complete {task_id}"], repo_dir)
    if commit.returncode != 0 and "nothing to commit" not in (commit.stdout + commit.stderr).lower():
        status = "failed"
        notes = f"git commit failed: {(commit.stderr or commit.stdout).strip()}"
    else:
        status = "done"
        notes = "Mock worker completed the task."
        if push_enabled:
            push = run_git(["git", "push", "--set-upstream", "origin", branch], repo_dir)
            if push.returncode != 0:
                status = "failed"
                notes = f"git push failed: {(push.stderr or push.stdout).strip()}"
            else:
                notes = "Mock worker completed the task and pushed the branch."
    base_commit = run_git(["git", "rev-parse", "HEAD~1"], repo_dir)
    head_commit = run_git(["git", "rev-parse", "HEAD"], repo_dir)
    base_rev = (base_commit.stdout or "").strip() or "unknown"
    head_rev = (head_commit.stdout or "").strip() or "unknown"
    rel_path = str(target.relative_to(repo_dir))
    return (
        "TASK_RESULT\n"
        f"task_id: {task_id}\n"
        f"status: {status}\n"
        f"branch: {branch}\n"
        f"base_commit: {base_rev}\n"
        f"head_commit: {head_rev}\n"
        "files_changed:\n"
        f"- {rel_path}\n"
        "verification:\n"
        "- mock worker created and committed the file\n"
        f"notes: {notes}\n"
    )


def main():
    job_id = os.environ["CODESWARM_JOB_ID"]
    node_id = int(os.environ["CODESWARM_NODE_ID"])
    base = Path(os.environ["CODESWARM_BASE_DIR"])
    inbox_path = base / "mailbox" / "inbox" / f"{job_id}_{node_id:02d}.jsonl"
    outbox_path = base / "mailbox" / "outbox" / f"{job_id}_{node_id:02d}.jsonl"
    agent_dir = Path.cwd()

    write_jsonl(outbox_path, {
        "type": "start",
        "job_id": job_id,
        "node_id": node_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mock_worker": True,
    })

    offset = 0
    while True:
        time.sleep(0.05)
        if not inbox_path.exists():
            continue
        size = inbox_path.stat().st_size
        if offset > size:
            offset = 0
        with inbox_path.open("rb") as f:
            f.seek(offset)
            while True:
                line = f.readline()
                if not line:
                    break
                if not line.endswith(b"\n"):
                    break
                offset = f.tell()
                try:
                    payload = json.loads(line.decode("utf-8"))
                except Exception:
                    continue
                if payload.get("type") != "user":
                    continue
                content = str(payload.get("content") or "")
                injection_id = payload.get("injection_id")
                write_jsonl(outbox_path, rpc_event(job_id, node_id, injection_id, "thread/status/changed", {
                    "status": {"type": "active"}
                }))
                write_jsonl(outbox_path, rpc_event(job_id, node_id, injection_id, "turn/started", {}))

                if "Return the task graph as JSON" in content or "TASK_GRAPH_JSON" in content:
                    response_text = build_task_graph_response(content)
                elif "You are executing orchestrated project task" in content:
                    repo_dir = agent_dir / "repo"
                    response_text = build_task_result(content, repo_dir)
                else:
                    response_text = "TASK_RESULT\ntask_id: UNKNOWN\nstatus: failed\nnotes: Unsupported mock prompt\n"

                write_jsonl(outbox_path, rpc_event(job_id, node_id, injection_id, "codex/event/agent_message", {
                    "msg": {
                        "phase": "final_answer",
                        "message": response_text,
                    }
                }))
                write_jsonl(outbox_path, rpc_event(job_id, node_id, injection_id, "codex/event/task_complete", {
                    "msg": {
                        "last_agent_message": response_text
                    }
                }))
                write_jsonl(outbox_path, rpc_event(job_id, node_id, injection_id, "turn/completed", {}))
                write_jsonl(outbox_path, rpc_event(job_id, node_id, injection_id, "thread/status/changed", {
                    "status": {"type": "idle"}
                }))


if __name__ == "__main__":
    main()
