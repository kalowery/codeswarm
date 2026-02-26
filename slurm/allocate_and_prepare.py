#!/usr/bin/env python3
import sys
import argparse
import subprocess
import time
from pathlib import Path

# Make project root importable
sys.path.append(str(Path(__file__).resolve().parents[1]))

from common.config import load_config


def run(cmd, input_text=None):
    return subprocess.run(cmd, input=input_text, capture_output=True, text=True)


def ssh_login(login_alias, cmd, input_text=None):
    return run(["ssh", login_alias, cmd], input_text=input_text)


def build_sbatch_script(args, config):
    workspace_root = config["cluster"]["workspace_root"]
    cluster_subdir = config["cluster"]["cluster_subdir"]
    hpc_base = f"{workspace_root}/{cluster_subdir}"

    lines = [
        "#!/bin/bash",
        "#SBATCH --job-name=codeswarm",
        f"#SBATCH --nodes={args.nodes}",
        f"#SBATCH --ntasks={args.nodes}",
        f"#SBATCH --time={args.time}",
        "#SBATCH --signal=TERM@60",
        f"#SBATCH --output={hpc_base}/codeswarm_%j.out",
        f"#SBATCH --error={hpc_base}/codeswarm_%j.err",
    ]

    slurm_cfg = config.get("cluster", {}).get("slurm", {})

    if args.partition:
        lines.append(f"#SBATCH --partition={args.partition}")
    elif slurm_cfg.get("default_partition"):
        lines.append(f"#SBATCH --partition={slurm_cfg.get('default_partition')}")

    if args.account:
        lines.append(f"#SBATCH --account={args.account}")
    elif slurm_cfg.get("default_account"):
        lines.append(f"#SBATCH --account={slurm_cfg.get('default_account')}")

    if args.qos:
        lines.append(f"#SBATCH --qos={args.qos}")
    elif slurm_cfg.get("default_qos"):
        lines.append(f"#SBATCH --qos={slurm_cfg.get('default_qos')}")

    lines.extend([
        "",
        f"mkdir -p {hpc_base}",
        f"cd {hpc_base}",
    ])

    if args.launch_codex_run:
        lines.extend([
            f"export WORKSPACE_ROOT={workspace_root}",
            f"export CLUSTER_SUBDIR={cluster_subdir}",
            f"srun python3 {hpc_base}/agent/codex_worker.py",
        ])

    elif args.launch_codex_test:
        lines.extend([
            f"export WORKSPACE_ROOT={workspace_root}",
            f"export CLUSTER_SUBDIR={cluster_subdir}",
            f"export NPM_CONFIG_PREFIX={hpc_base}/tools/npm-global",
            f"srun --ntasks-per-node=1 {hpc_base}/tools/npm-global/bin/codex --ask-for-approval never exec --skip-git-repo-check \"Say hello in one short sentence.\"",
        ])

    else:
        lines.extend([
            "while true; do",
            "  sleep 300",
            "done",
        ])

    return "\n".join(lines) + "\n"


def submit_job(args, config):
    login_alias = config["ssh"]["login_alias"]

    script = build_sbatch_script(args, config)

    print("----- SBATCH SCRIPT BEGIN -----")
    print(script)
    print("----- SBATCH SCRIPT END -----")

    result = ssh_login(login_alias, "sbatch", input_text=script)

    if result.returncode != 0:
        print(result.stderr)
        sys.exit(1)

    for token in result.stdout.split():
        if token.isdigit():
            return token

    raise RuntimeError("Failed to parse job ID")


def wait_running(job_id, config):
    login_alias = config["ssh"]["login_alias"]

    while True:
        result = ssh_login(login_alias, f"squeue -j {job_id} -h -o %T")
        state = result.stdout.strip()

        if state == "RUNNING":
            return

        if state in ("FAILED", "CANCELLED", "TIMEOUT", "NODE_FAIL"):
            raise RuntimeError(f"Job entered terminal state: {state}")

        if state == "":
            return

        time.sleep(3)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--nodes", type=int, default=1)
    parser.add_argument("--time")
    parser.add_argument("--partition")
    parser.add_argument("--account")
    parser.add_argument("--qos")
    parser.add_argument("--launch-codex-test", action="store_true")
    parser.add_argument("--launch-codex-run", action="store_true")

    args = parser.parse_args()
    config = load_config(args.config)

    if not args.time:
        slurm_cfg = config.get("cluster", {}).get("slurm", {})
        args.time = slurm_cfg.get("time_limit") or slurm_cfg.get("default_time")

    workspace_root = config["cluster"]["workspace_root"]
    cluster_subdir = config["cluster"]["cluster_subdir"]
    hpc_base = f"{workspace_root}/{cluster_subdir}"
    login_alias = config["ssh"]["login_alias"]

    # ✅ DEPLOY AGENT DIRECTORY (worker + follower) BEFORE SUBMISSION
    if args.launch_codex_run:
        agent_local_dir = Path(__file__).parent.parent / "agent"
        agent_remote_dir = f"{hpc_base}/agent"

        # Ensure remote directory exists
        ssh_login(login_alias, f"mkdir -p {agent_remote_dir}")

        # Rsync entire agent directory (codex_worker.py, outbox_follower.py, future utilities)
        subprocess.run(
            [
                "rsync",
                "-az",
                str(agent_local_dir) + "/",
                f"{login_alias}:{agent_remote_dir}/",
            ],
            check=True,
        )

    print("Submitting allocation job...")
    job_id = submit_job(args, config)
    print(f"Submitted job {job_id}")

    # ✅ CREATE RUN DIRECTORIES AFTER JOB ID EXISTS
    if args.launch_codex_run:
        run_base = f"{hpc_base}/runs/{job_id}"
        ssh_login(login_alias, f"mkdir -p {run_base}")

        for i in range(args.nodes):
            node_dir = f"{run_base}/node_{i:02d}"
            ssh_login(login_alias, f"mkdir -p {node_dir}")
            ssh_login(
                login_alias,
                f'echo "Node {i}: Say hello in one short sentence." > {node_dir}/PROMPT.txt'
            )

    print("Waiting for RUNNING state...")
    wait_running(job_id, config)

    print("Allocation complete.")
    print(f"JOB_ID={job_id}")
