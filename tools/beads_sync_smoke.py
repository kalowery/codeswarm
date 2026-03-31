#!/usr/bin/env python3
import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(ROOT))
from router import router as router_module


def require_beads():
    if shutil.which("bd") or shutil.which("beads"):
        return
    raise SystemExit("bd CLI not installed")


def git(args: list[str], cwd: Path, env: dict | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env)


def make_remote_repo(prefix: str) -> tuple[Path, Path, Path]:
    root_dir = Path(tempfile.mkdtemp(prefix=prefix))
    origin_dir = root_dir / "origin.git"
    work_dir = root_dir / "work"
    subprocess.run(["git", "init", "--bare", str(origin_dir)], check=True, capture_output=True, text=True)
    subprocess.run(["git", "clone", str(origin_dir), str(work_dir)], check=True, capture_output=True, text=True)
    git(["config", "user.email", "smoke@example.test"], cwd=work_dir)
    git(["config", "user.name", "Codeswarm Smoke"], cwd=work_dir)
    (work_dir / "README.md").write_text("# Beads Smoke Repo\n", encoding="utf-8")
    git(["add", "README.md"], cwd=work_dir)
    git(["commit", "-m", "init"], cwd=work_dir)
    git(["push", "-u", "origin", "HEAD:main"], cwd=work_dir)
    git(["checkout", "-B", "main"], cwd=work_dir)
    return root_dir, origin_dir, work_dir


def make_task(task_id: str, title: str, prompt: str, depends_on: list[str]) -> dict:
    now = time.time()
    return {
        "task_id": task_id,
        "title": title,
        "prompt": prompt,
        "acceptance_criteria": [f"{task_id} is represented in Beads"],
        "depends_on": list(depends_on),
        "owned_paths": [f"mock/{task_id}.txt"],
        "expected_touch_paths": [],
        "status": "pending",
        "attempts": 0,
        "assigned_swarm_id": None,
        "assigned_node_id": None,
        "assignment_injection_id": None,
        "branch": None,
        "result_status": None,
        "result_raw": None,
        "last_error": None,
        "beads_id": None,
        "beads_sync_status": "pending",
        "beads_last_error": None,
        "beads_dependencies_synced": [],
        "created_at": now,
        "updated_at": now,
    }


def make_project(repo_dir: Path) -> dict:
    now = time.time()
    tasks = {
        "T-001": make_task("T-001", "Create first smoke bead", "Create the first test task bead.", []),
        "T-002": make_task("T-002", "Create second smoke bead", "Create the dependent task bead.", ["T-001"]),
    }
    return {
        "project_id": f"project-{uuid.uuid4().hex[:8]}",
        "title": "Beads Sync Smoke",
        "repo_path": str(repo_dir),
        "base_branch": "main",
        "worker_swarm_ids": ["smoke-worker"],
        "status": "draft",
        "workspace_subdir": "repo",
        "repo_preparation": {},
        "task_order": ["T-001", "T-002"],
        "tasks": tasks,
        "beads_sync_status": "pending",
        "beads_last_error": None,
        "beads_repo_path": None,
        "beads_prefix": None,
        "beads_db_path": None,
        "beads_root_id": None,
        "created_at": now,
        "updated_at": now,
    }


def show_issue(repo_dir: Path, issue_id: str) -> dict:
    completed = subprocess.run(
        ["bd", "show", issue_id, "--json"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
        text=True,
        env=router_module._beads_env(),
    )
    payload = router_module._parse_json_output(completed.stdout)
    if not isinstance(payload, list) or not payload:
        raise RuntimeError(f"Unable to read issue {issue_id}")
    return payload[0]


def parse_jsonl(path: Path) -> list[dict]:
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        snippet = line.strip()
        if not snippet:
            continue
        records.append(json.loads(snippet))
    return records


def assert_remote_control_branch(project: dict, origin_dir: Path) -> list[dict]:
    branch = router_module._project_beads_control_branch(project)
    ls_remote = subprocess.run(
        ["git", "ls-remote", "--heads", str(origin_dir), branch],
        check=True,
        capture_output=True,
        text=True,
    )
    if branch not in ls_remote.stdout:
        raise RuntimeError(f"Expected control branch {branch} to be pushed")

    checkout_dir = Path(tempfile.mkdtemp(prefix="codeswarmbeadscontrol"))
    subprocess.run(
        ["git", "clone", "--branch", branch, str(origin_dir), str(checkout_dir)],
        check=True,
        capture_output=True,
        text=True,
    )
    issues_path = checkout_dir / ".beads" / "issues.jsonl"
    if not issues_path.exists():
        raise RuntimeError("Expected .beads/issues.jsonl on control branch")
    records = parse_jsonl(issues_path)
    if not records:
        raise RuntimeError("Expected Beads export to contain records")
    shutil.rmtree(checkout_dir, ignore_errors=True)
    return records


def assert_bootstrap_from_control_branch(origin_dir: Path, project: dict, issue_id: str):
    branch = router_module._project_beads_control_branch(project)
    recovery_dir = Path(tempfile.mkdtemp(prefix="codeswarmbeadsrecovery"))
    try:
        subprocess.run(
            ["git", "clone", "--branch", branch, str(origin_dir), str(recovery_dir)],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["bd", "bootstrap"],
            cwd=recovery_dir,
            check=True,
            capture_output=True,
            text=True,
            env=router_module._beads_env(),
        )
        recovered = show_issue(recovery_dir, issue_id)
        if recovered.get("id") != issue_id:
            raise RuntimeError("Recovered Beads database did not contain expected issue")
    finally:
        shutil.rmtree(recovery_dir, ignore_errors=True)


def run_disabled_case():
    root_dir, _, repo_dir = make_remote_repo("codeswarmbeadsdisabled")
    project = make_project(repo_dir)
    original_disable = os.environ.get("CODESWARM_DISABLE_BEADS_SYNC")
    try:
        os.environ["CODESWARM_DISABLE_BEADS_SYNC"] = "1"
        router_module._sync_project_to_beads(project)
    finally:
        if original_disable is None:
            os.environ.pop("CODESWARM_DISABLE_BEADS_SYNC", None)
        else:
            os.environ["CODESWARM_DISABLE_BEADS_SYNC"] = original_disable
        shutil.rmtree(root_dir, ignore_errors=True)
    if project.get("beads_sync_status") != "disabled":
        raise RuntimeError(f"Expected disabled Beads status, got {project.get('beads_sync_status')}")


def run_enabled_case():
    root_dir, origin_dir, repo_dir = make_remote_repo("codeswarmbeadsenabled")
    beads_home = Path(tempfile.mkdtemp(prefix="codeswarmbeadshome"))
    project = make_project(repo_dir)
    original_disable = os.environ.get("CODESWARM_DISABLE_BEADS_SYNC")
    original_home = os.environ.get("CODESWARM_BEADS_HOME")
    try:
        os.environ.pop("CODESWARM_DISABLE_BEADS_SYNC", None)
        os.environ["CODESWARM_BEADS_HOME"] = str(beads_home)
        router_module._sync_project_to_beads(project)

        if project.get("beads_sync_status") != "synced":
            raise RuntimeError(f"Expected synced project status, got {project.get('beads_sync_status')} / {project.get('beads_last_error')}")
        if not project.get("beads_root_id"):
            raise RuntimeError("Project root bead was not created")

        task_one = project["tasks"]["T-001"]
        task_two = project["tasks"]["T-002"]
        if not task_one.get("beads_id") or not task_two.get("beads_id"):
            raise RuntimeError("Task beads were not created")

        exported_records = assert_remote_control_branch(project, origin_dir)
        exported_ids = {str(record.get("id") or "") for record in exported_records if isinstance(record, dict)}
        expected_ids = {project["beads_root_id"], task_one["beads_id"], task_two["beads_id"]}
        if not expected_ids.issubset(exported_ids):
            raise RuntimeError("Control branch export is missing expected Beads records")

        issue_two = show_issue(repo_dir, task_two["beads_id"])
        dependencies = issue_two.get("dependencies") or []
        if not any(dep.get("id") == task_one["beads_id"] for dep in dependencies if isinstance(dep, dict)):
            raise RuntimeError("Dependent task bead did not receive dependency link")

        task_one["status"] = "assigned"
        router_module._sync_task_status_to_beads(project, task_one)
        if show_issue(repo_dir, task_one["beads_id"]).get("status") != "in_progress":
            raise RuntimeError("Assigned task bead did not move to in_progress")

        task_one["status"] = "completed"
        task_one["result_status"] = "done"
        task_one["branch"] = "codeswarm/test/t-001"
        router_module._sync_task_status_to_beads(project, task_one)
        if show_issue(repo_dir, task_one["beads_id"]).get("status") != "closed":
            raise RuntimeError("Completed task bead did not close")

        exported_records = assert_remote_control_branch(project, origin_dir)
        exported_task_one = next(
            (record for record in exported_records if str(record.get("id") or "") == task_one["beads_id"]),
            None,
        )
        if not isinstance(exported_task_one, dict) or exported_task_one.get("status") != "closed":
            raise RuntimeError("Control branch export did not capture the closed task status")

        assert_bootstrap_from_control_branch(origin_dir, project, task_one["beads_id"])
    finally:
        if original_disable is None:
            os.environ.pop("CODESWARM_DISABLE_BEADS_SYNC", None)
        else:
            os.environ["CODESWARM_DISABLE_BEADS_SYNC"] = original_disable
        if original_home is None:
            os.environ.pop("CODESWARM_BEADS_HOME", None)
        else:
            os.environ["CODESWARM_BEADS_HOME"] = original_home
        shutil.rmtree(root_dir, ignore_errors=True)
        shutil.rmtree(beads_home, ignore_errors=True)


def main():
    require_beads()
    run_disabled_case()
    run_enabled_case()
    print("beads sync smoke passed")


if __name__ == "__main__":
    main()
