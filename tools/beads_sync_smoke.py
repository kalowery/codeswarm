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


def make_repo(prefix: str) -> Path:
    repo_dir = Path(tempfile.mkdtemp(prefix=prefix))
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_dir, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "smoke@example.test"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "Codeswarm Smoke"], cwd=repo_dir, check=True)
    (repo_dir / "README.md").write_text("# Beads Smoke Repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo_dir, check=True, capture_output=True, text=True)
    return repo_dir


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


def run_disabled_case():
    repo_dir = make_repo("codeswarmbeadsdisabled")
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
        shutil.rmtree(repo_dir, ignore_errors=True)
    if project.get("beads_sync_status") != "disabled":
        raise RuntimeError(f"Expected disabled Beads status, got {project.get('beads_sync_status')}")


def run_enabled_case():
    repo_dir = make_repo("codeswarmbeadsenabled")
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
    finally:
        if original_disable is None:
            os.environ.pop("CODESWARM_DISABLE_BEADS_SYNC", None)
        else:
            os.environ["CODESWARM_DISABLE_BEADS_SYNC"] = original_disable
        if original_home is None:
            os.environ.pop("CODESWARM_BEADS_HOME", None)
        else:
            os.environ["CODESWARM_BEADS_HOME"] = original_home
        shutil.rmtree(repo_dir, ignore_errors=True)
        shutil.rmtree(beads_home, ignore_errors=True)


def main():
    require_beads()
    run_disabled_case()
    run_enabled_case()
    print("beads sync smoke passed")


if __name__ == "__main__":
    main()
