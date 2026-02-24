#!/usr/bin/env python3
import os
import json
import subprocess
import time
from pathlib import Path


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def main():
    job_id = os.environ.get("SLURM_JOB_ID")
    node_id = int(os.environ.get("SLURM_NODEID", "0"))
    hostname = os.uname().nodename

    workspace_root = os.environ["WORKSPACE_ROOT"]
    cluster_subdir = os.environ["CLUSTER_SUBDIR"]

    base = Path(workspace_root) / cluster_subdir
    run_dir = base / "runs" / job_id
    node_dir = run_dir / f"node_{node_id:02d}"

    prompt_path = node_dir / "PROMPT.txt"
    result_path = node_dir / "result.json"

    # ✅ Wait for PROMPT.txt to appear (control-plane sync)
    for _ in range(30):  # wait up to 30 seconds
        if prompt_path.exists():
            break
        time.sleep(1)
    else:
        raise RuntimeError(f"Missing PROMPT.txt for node {node_id}")

    prompt = prompt_path.read_text()

    codex_bin = base / "tools" / "npm-global" / "bin" / "codex"

    result = run([
        str(codex_bin),
        "--ask-for-approval", "never",
        "exec",
        "--skip-git-repo-check",
        prompt
    ])

    output = {
        "job_id": job_id,
        "node_id": node_id,
        "hostname": hostname,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }

    result_path.write_text(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
