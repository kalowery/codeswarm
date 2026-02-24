#!/usr/bin/env python3
import argparse
import subprocess
import time
import sys
from pathlib import Path
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
        "#SBATCH --job-name=codex-alloc",
        f"#SBATCH --nodes={args.nodes}",
        f"#SBATCH --time={args.time}",
        f"#SBATCH --output={hpc_base}/codex_alloc_%j.out",
        f"#SBATCH --error={hpc_base}/codex_alloc_%j.err",
    ]

    if args.partition:
        lines.append(f"#SBATCH --partition={args.partition}")
    elif config["slurm"]["default_partition"]:
        lines.append(f"#SBATCH --partition={config['slurm']['default_partition']}")

    if args.account:
        lines.append(f"#SBATCH --account={args.account}")
    elif config["slurm"]["default_account"]:
        lines.append(f"#SBATCH --account={config['slurm']['default_account']}")

    if args.qos:
        lines.append(f"#SBATCH --qos={args.qos}")
    elif config["slurm"]["default_qos"]:
        lines.append(f"#SBATCH --qos={config['slurm']['default_qos']}")

    workspace_root = config["cluster"]["workspace_root"]
    cluster_subdir = config["cluster"]["cluster_subdir"]
    hpc_base = f"{workspace_root}/{cluster_subdir}"

    if args.launch_worker:
        lines.extend([
            "",
            f"mkdir -p {hpc_base}",
            f"cd {hpc_base}",
            f"export WORKSPACE_ROOT={workspace_root}",
            f"export CLUSTER_SUBDIR={cluster_subdir}",
            f"srun --ntasks-per-node=1 python3 {hpc_base}/agent/worker.py",
        ])
    else:
        lines.extend([
            "",
            f"mkdir -p {hpc_base}",
            f"cd {hpc_base}",
            "while true; do",
            "  sleep 300",
            "done",
        ])


    return "\n".join(lines) + "\n"


def submit_job(args, config):
    login_alias = config["ssh"]["login_alias"]
    script = build_sbatch_script(args, config)
    if args.launch_worker:
        login_alias = config["ssh"]["login_alias"]
        workspace_root = config["cluster"]["workspace_root"]
        cluster_subdir = config["cluster"]["cluster_subdir"]
        hpc_base = f"{workspace_root}/{cluster_subdir}"

        worker_local = Path(__file__).parent.parent / "agent" / "worker.py"
        worker_remote = f"{hpc_base}/agent/worker.py"

        # Ensure agent directory exists
        ssh_login(login_alias, f"mkdir -p {hpc_base}/agent")

        # Copy worker file via SSH pipe
        with open(worker_local, "r") as f:
            subprocess.run(
                ["ssh", login_alias, f"cat > {worker_remote}"],
                input=f.read(),
                text=True,
                check=True
            )

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

        time.sleep(3)


def get_nodes(job_id, config):
    login_alias = config["ssh"]["login_alias"]
    cmd = f"scontrol show hostnames $(squeue -j {job_id} -h -o %N)"
    result = ssh_login(login_alias, cmd)

    if result.returncode != 0:
        raise RuntimeError(result.stderr)

    return sorted(result.stdout.strip().split())


def resolve_ips(nodes, config):
    login_alias = config["ssh"]["login_alias"]
    cmd = f"getent hosts {' '.join(nodes)}"
    result = ssh_login(login_alias, cmd)

    if result.returncode != 0:
        raise RuntimeError(result.stderr)

    mapping = {}
    for line in result.stdout.strip().splitlines():
        parts = line.split()
        if len(parts) >= 2:
            ip, host = parts[0], parts[1]
            mapping[host] = ip

    if len(mapping) != len(nodes):
        raise RuntimeError("IP resolution mismatch")

    return mapping


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--nodes", type=int, default=1)
    parser.add_argument("--time")
    parser.add_argument("--partition")
    parser.add_argument("--account")
    parser.add_argument("--qos")
    parser.add_argument("--launch-worker", action="store_true")
    args = parser.parse_args()

    config = load_config(args.config)

    if not args.time:
        args.time = config["slurm"]["default_time"]

    print("Submitting allocation job...")
    job_id = submit_job(args, config)
    print(f"Submitted job {job_id}")

    print("Waiting for RUNNING state...")
    wait_running(job_id, config)

    print("Discovering nodes...")
    nodes = get_nodes(job_id, config)
    print(f"Allocated nodes: {nodes}")

    print("Resolving IP addresses...")
    node_ip_map = resolve_ips(nodes, config)

    print("Updating SSH config...")
    subprocess.run([
        "python3",
        str(Path(__file__).parent.parent / "ssh" / "update_ssh_config.py"),
        args.config,
        job_id,
        *[f"{k}={v}" for k, v in node_ip_map.items()]
    ], check=True)

    print("Allocation + SSH configuration complete.")
    print(f"JOB_ID={job_id}")
