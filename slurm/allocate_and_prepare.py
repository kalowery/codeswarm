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
            "",
            "# Per-node working directory isolation (use existing runs/<job_id>/node_XX layout)",
            f"srun bash -c '\n"
            f"NODE_INDEX=$(printf \"%02d\" $SLURM_PROCID)\n"
            f"NODE_WORKDIR=\"{hpc_base}/runs/$SLURM_JOB_ID/node_${{NODE_INDEX}}\"\n"
            f"mkdir -p \"$NODE_WORKDIR\"\n"
            f"cd \"$NODE_WORKDIR\"\n"
            f"python3 {hpc_base}/agent/codex_worker.py\n"
            f"'",
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


def ensure_codex_ready(config):
    login_alias = config["ssh"]["login_alias"]
    workspace_root = config["cluster"]["workspace_root"]
    cluster_subdir = config["cluster"]["cluster_subdir"]

    tools_dir = f"{workspace_root}/{cluster_subdir}/tools"
    node_bin = f"{tools_dir}/node/bin"
    npm_bin = f"{node_bin}/npm"
    npm_prefix = f"{tools_dir}/npm-global"
    codex_bin = f"{npm_prefix}/bin/codex"

    # Ensure Node/npm exist in workspace
    if ssh_login(login_alias, f'test -x "{npm_bin}"').returncode != 0:
        print("ERROR: Node/npm not found in workspace tools directory.")
        print(f"Expected npm at: {npm_bin}")
        sys.exit(1)

    # Ensure codex installed in workspace (version pinned)
    CODEX_VERSION = "latest"  # change to explicit version if desired, e.g. "1.2.3"

    if ssh_login(login_alias, f'test -x "{codex_bin}"').returncode != 0:
        print("Codex not found in workspace. Installing...")
        install_cmd = (
            f'NPM_CONFIG_PREFIX="{npm_prefix}" '
            f'"{npm_bin}" install -g @openai/codex@{CODEX_VERSION}'
        )
        result = ssh_login(login_alias, install_cmd)
        if result.returncode != 0:
            print("Codex installation failed:")
            print(result.stderr)
            sys.exit(1)

    # Verify installed codex version (defensive check)
    version_check = ssh_login(login_alias, f'"{codex_bin}" --version')
    if version_check.returncode != 0:
        print("ERROR: Codex binary exists but failed to execute --version.")
        print(version_check.stderr)
        sys.exit(1)

    # Check for existing Codex home configuration
    codex_home_check = ssh_login(
        login_alias,
        'test -d "$HOME/.codex"'
    )

    if codex_home_check.returncode == 0:
        print("Existing ~/.codex detected. Skipping login.")
        return

    print("No ~/.codex directory found. Codex not authenticated.")

    # Ensure OPENAI_API_KEY exists on login node
    key_check = ssh_login(login_alias, 'test -n "$OPENAI_API_KEY"')

    if key_check.returncode != 0:
        print("\nERROR:")
        print("Codex not authenticated and OPENAI_API_KEY is not set.")
        print("On the login node, run:")
        print("  export OPENAI_API_KEY=sk-...")
        print("Then re-run codeswarm.\n")
        sys.exit(1)

    print("Authenticating Codex using OPENAI_API_KEY...")
    login_result = ssh_login(
        login_alias,
        f'printf "%s" "$OPENAI_API_KEY" | "{codex_bin}" login --with-api-key'
    )

    if login_result.returncode != 0:
        print("Codex login failed:")
        print(login_result.stderr)
        sys.exit(1)

    # Verify ~/.codex now exists
    verify_home = ssh_login(
        login_alias,
        'test -d "$HOME/.codex"'
    )

    if verify_home.returncode != 0:
        print("Codex login did not create ~/.codex.")
        sys.exit(1)

    print("Codex authenticated successfully.")


def ensure_partition_capacity(args, config):
    login_alias = config["ssh"]["login_alias"]

    slurm_cfg = config.get("cluster", {}).get("slurm", {})

    partition = (
        args.partition
        or slurm_cfg.get("default_partition")
    )

    if not partition:
        print("No partition specified and no default partition configured.")
        sys.exit(1)

    result = ssh_login(
        login_alias,
        f'sinfo -h -p {partition} -o "%D %t"'
    )

    if result.returncode != 0:
        print("Failed to query partition state via sinfo:")
        print(result.stderr)
        sys.exit(1)

    idle_nodes = 0

    for line in result.stdout.strip().splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue

        count, state = parts

        if state.lower().startswith("idle"):
            try:
                idle_nodes += int(count)
            except Exception:
                pass

    if idle_nodes < args.nodes:
        print("\nERROR:")
        print(f"Partition '{partition}' has {idle_nodes} idle nodes.")
        print(f"Requested {args.nodes} nodes.")
        print("Not submitting job.\n")
        sys.exit(1)

    print(f"Partition '{partition}' has {idle_nodes} idle nodes. OK.")


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

    # Ensure Codex installed and authenticated before submission
    if args.launch_codex_run:
        ensure_codex_ready(config)

    # Ensure partition has sufficient idle nodes
    ensure_partition_capacity(args, config)

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
