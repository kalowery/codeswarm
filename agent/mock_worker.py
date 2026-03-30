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


def extract_section_bullets(text: str, header: str) -> list[str]:
    lines = text.splitlines()
    capture = False
    results: list[str] = []
    header_text = header.strip()
    header_variants = {header_text, header_text.rstrip(":")}
    for raw_line in lines:
        line = raw_line.rstrip()
        if line.strip() in header_variants:
            capture = True
            continue
        if not capture:
            continue
        if not line.strip():
            break
        if line.lstrip().startswith("- "):
            results.append(line.lstrip()[2:].strip())
            continue
        break
    return results


def extract_task_prompt_text(text: str) -> str:
    marker = "Task prompt:\n"
    start = text.find(marker)
    if start < 0:
        return ""
    start += len(marker)
    end_markers = ("\n\nDependencies:\n", "\n\nAcceptance criteria:\n")
    end_positions = [text.find(marker, start) for marker in end_markers if text.find(marker, start) >= 0]
    end = min(end_positions) if end_positions else len(text)
    return text[start:end].strip()


def extract_prompt_file_targets(task_prompt: str) -> list[tuple[str, str]]:
    if not isinstance(task_prompt, str) or not task_prompt.strip():
        return []
    patterns = [
        re.compile(
            r"create(?:s)? (?:a|the) file [`'\"](?P<path>[^`'\"]+)[`'\"] containing exactly [`'\"](?P<content>[^`'\"]*)[`'\"]",
            re.IGNORECASE,
        ),
        re.compile(
            r"write [`'\"](?P<content>[^`'\"]*)[`'\"] to [`'\"](?P<path>[^`'\"]+)[`'\"]",
            re.IGNORECASE,
        ),
    ]
    targets: list[tuple[str, str]] = []
    for pattern in patterns:
        for match in pattern.finditer(task_prompt):
            rel_path = str(match.group("path") or "").strip()
            content = str(match.group("content") or "")
            if rel_path:
                targets.append((rel_path, content))
        if targets:
            break
    return targets


def extract_task_graph_override(spec_text: str):
    marker = "MOCK_TASK_GRAPH_JSON:"
    idx = spec_text.find(marker)
    if idx < 0:
        return None
    raw = spec_text[idx + len(marker):].strip()
    try:
        parsed, _ = json.JSONDecoder().raw_decode(raw)
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


def current_head(repo_dir: Path, rev: str = "HEAD") -> str:
    completed = run_git(["git", "rev-parse", rev], repo_dir)
    return (completed.stdout or "").strip() or "unknown"


def gather_changed_files(repo_dir: Path, base_rev: str, head_rev: str) -> list[str]:
    completed = run_git(["git", "diff", "--name-only", base_rev, head_rev], repo_dir)
    if completed.returncode != 0:
        return []
    return [line.strip() for line in (completed.stdout or "").splitlines() if line.strip()]


def build_mock_integration_result(content: str, repo_dir: Path) -> str:
    task_id = extract_between(content, "You are executing orchestrated project task ").split(".")[0].strip() or "T-UNKNOWN"
    branch = extract_between(content, "Working branch to create/use: ") or "codeswarm/mock/integration"
    base_branch = extract_between(content, "Base branch: ") or "main"
    branch_specs = extract_section_bullets(content, "Integration source branches:")
    source_branches = []
    for item in branch_specs:
        if ":" in item:
            _, branch_name = item.split(":", 1)
            branch_name = branch_name.strip()
        else:
            branch_name = item.strip()
        if branch_name:
            source_branches.append(branch_name)

    push_enabled = str(os.environ.get("CODESWARM_MOCK_PUSH_BRANCHES") or "").strip().lower() in ("1", "true", "yes", "on")
    ensure_git_identity(repo_dir)

    branch_existed = run_git(["git", "rev-parse", "--verify", branch], repo_dir).returncode == 0
    checkout = run_git(["git", "checkout", "-B", branch, base_branch], repo_dir)
    if checkout.returncode != 0:
        notes = f"git checkout failed: {(checkout.stderr or checkout.stdout).strip()}"
        base_rev = current_head(repo_dir)
        return (
            "TASK_RESULT\n"
            f"task_id: {task_id}\n"
            "status: failed\n"
            f"branch: {branch}\n"
            f"base_commit: {base_rev}\n"
            f"head_commit: {base_rev}\n"
            "files_changed:\n"
            "- none\n"
            "verification:\n"
            "- integration branch checkout attempted\n"
            f"notes: {notes}\n"
        )

    base_rev = current_head(repo_dir, "HEAD")
    verification = []
    status = "done"
    notes = "Mock worker merged task branches into the integration branch."

    if branch_existed:
        verification.append("integration branch reset from base branch")
    else:
        verification.append("integration branch created from base branch")

    for source_branch in source_branches:
        has_branch = run_git(["git", "rev-parse", "--verify", source_branch], repo_dir).returncode == 0
        if not has_branch:
            fetch = run_git(["git", "fetch", "origin", f"{source_branch}:{source_branch}"], repo_dir)
            verification.append(f"fetched {source_branch} from origin")
            if fetch.returncode != 0:
                status = "blocked"
                notes = f"git fetch failed for {source_branch}: {(fetch.stderr or fetch.stdout).strip()}"
                break
        merge = run_git(["git", "merge", "--no-ff", "--no-edit", source_branch], repo_dir)
        verification.append(f"merged {source_branch}")
        if merge.returncode != 0:
            status = "blocked"
            notes = f"git merge failed for {source_branch}: {(merge.stderr or merge.stdout).strip()}"
            run_git(["git", "merge", "--abort"], repo_dir)
            break

    if status == "done" and push_enabled:
        push = run_git(["git", "push", "--set-upstream", "origin", branch], repo_dir)
        if push.returncode != 0:
            status = "failed"
            notes = f"git push failed: {(push.stderr or push.stdout).strip()}"
        else:
            verification.append("pushed integration branch")

    head_rev = current_head(repo_dir, "HEAD")
    changed_files = gather_changed_files(repo_dir, base_rev, head_rev)
    file_lines = changed_files or ["none"]
    verification_lines = verification or ["integration branch prepared"]
    return (
        "TASK_RESULT\n"
        f"task_id: {task_id}\n"
        f"status: {status}\n"
        f"branch: {branch}\n"
        f"base_commit: {base_rev}\n"
        f"head_commit: {head_rev}\n"
        "files_changed:\n"
        + "".join(f"- {item}\n" for item in file_lines)
        + "verification:\n"
        + "".join(f"- {item}\n" for item in verification_lines)
        + f"notes: {notes}\n"
    )


def build_task_result(content: str, repo_dir: Path) -> str:
    task_id = extract_between(content, "You are executing orchestrated project task ").split(".")[0].strip() or "T-UNKNOWN"
    branch = extract_between(content, "Working branch to create/use: ") or f"codeswarm/mock/{task_id.lower()}"
    title = extract_between(content, "Task title: ") or task_id
    task_prompt = extract_task_prompt_text(content)
    prompt_targets = extract_prompt_file_targets(task_prompt)
    push_enabled = str(os.environ.get("CODESWARM_MOCK_PUSH_BRANCHES") or "").strip().lower() in ("1", "true", "yes", "on")
    ensure_git_identity(repo_dir)
    run_git(["git", "checkout", "-B", branch], repo_dir)
    changed_paths: list[str] = []
    if prompt_targets:
        for rel_path, file_content in prompt_targets:
            target = repo_dir / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(file_content, encoding="utf-8")
            rel_text = str(target.relative_to(repo_dir))
            changed_paths.append(rel_text)
            run_git(["git", "add", rel_text], repo_dir)
    else:
        task_dir = repo_dir / "mock_tasks"
        task_dir.mkdir(parents=True, exist_ok=True)
        filename_token = re.sub(r"[^a-zA-Z0-9._-]+", "_", task_id).strip("_") or "task"
        target = task_dir / f"{filename_token}.txt"
        target.write_text(f"{task_id}\n{title}\n", encoding="utf-8")
        rel_text = str(target.relative_to(repo_dir))
        changed_paths.append(rel_text)
        run_git(["git", "add", rel_text], repo_dir)
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
    return (
        "TASK_RESULT\n"
        f"task_id: {task_id}\n"
        f"status: {status}\n"
        f"branch: {branch}\n"
        f"base_commit: {base_rev}\n"
        f"head_commit: {head_rev}\n"
        "files_changed:\n"
        + "".join(f"- {rel_path}\n" for rel_path in changed_paths)
        + "verification:\n"
        + ("- mock worker created prompt-directed file(s) and committed them\n" if prompt_targets else "- mock worker created and committed the file\n")
        + f"notes: {notes}\n"
    )


def build_generic_response(content: str) -> str:
    text = " ".join(str(content or "").split())
    if len(text) > 180:
        text = text[:177] + "..."
    return f"Mock worker received prompt: {text}"


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
    try:
        response_delay_ms = max(0, int(os.environ.get("CODESWARM_MOCK_DELAY_MS") or "0"))
    except Exception:
        response_delay_ms = 0

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

                if response_delay_ms > 0:
                    time.sleep(response_delay_ms / 1000.0)

                if "Return the task graph as JSON" in content or "TASK_GRAPH_JSON" in content:
                    response_text = build_task_graph_response(content)
                elif "You are executing orchestrated project task" in content:
                    repo_dir = agent_dir / "repo"
                    if "This is the final integration task for the project." in content:
                        response_text = build_mock_integration_result(content, repo_dir)
                    else:
                        response_text = build_task_result(content, repo_dir)
                else:
                    response_text = build_generic_response(content)

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
