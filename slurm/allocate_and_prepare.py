#!/usr/bin/env python3
import sys
import argparse
import subprocess
import time
import base64
import shlex
import json
import os
import shutil
from pathlib import Path
from pathlib import PurePosixPath

# Make project root importable
sys.path.append(str(Path(__file__).resolve().parents[1]))

from common.config import load_config

MIN_PYTHON = (3, 10)
if sys.version_info < MIN_PYTHON and os.environ.get("CODESWARM_PY_REEXEC") != "1":
    candidates = ["python3.13", "python3.12", "python3.11", "python3.10", "python3", "python"]
    for candidate in candidates:
        candidate_path = shutil.which(candidate)
        if not candidate_path:
            continue
        probe = subprocess.run(
            [candidate_path, "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
            capture_output=True,
            text=True,
        )
        if probe.returncode != 0:
            continue
        try:
            major, minor = [int(part) for part in probe.stdout.strip().split(".")[:2]]
        except Exception:
            continue
        if (major, minor) >= MIN_PYTHON:
            os.environ["CODESWARM_PY_REEXEC"] = "1"
            os.execv(candidate_path, [candidate_path, *sys.argv])

if sys.version_info < MIN_PYTHON:
    print(
        f"ERROR: slurm/allocate_and_prepare.py requires Python "
        f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]}+; found "
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}. "
        f"No suitable python3.10+ interpreter was auto-discovered."
    )
    sys.exit(1)


def run(cmd, input_text=None):
    return subprocess.run(cmd, input=input_text, capture_output=True, text=True)


def ssh_login(login_host, cmd, input_text=None):
    return run(["ssh", login_host, cmd], input_text=input_text)


def resolve_login_host(config):
    cluster_cfg = config.get("cluster", {})
    slurm_cfg = cluster_cfg.get("slurm", {}) if isinstance(cluster_cfg, dict) else {}
    host = slurm_cfg.get("login_host")
    if isinstance(host, str) and host.strip():
        return host.strip()
    legacy = slurm_cfg.get("login_alias")
    if isinstance(legacy, str) and legacy.strip():
        return legacy.strip()
    raise RuntimeError("Slurm login_host not configured in cluster.slurm")


def resolve_slurm_paths(config):
    cluster_cfg = config.get("cluster", {})
    slurm_cfg = cluster_cfg.get("slurm", {}) if isinstance(cluster_cfg, dict) else {}
    workspace_root = slurm_cfg.get("workspace_root") or cluster_cfg.get("workspace_root")
    cluster_subdir = slurm_cfg.get("cluster_subdir") or cluster_cfg.get("cluster_subdir")
    return workspace_root, cluster_subdir


def safe_skill_rel_path(path: str) -> str | None:
    try:
        parts = PurePosixPath(path).parts
    except Exception:
        return None
    if not parts:
        return None
    if any(part in ("", ".", "..") for part in parts):
        return None
    return str(PurePosixPath(*parts))


def build_sbatch_script(args, config):
    workspace_root, cluster_subdir = resolve_slurm_paths(config)
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
            "",
            "# Per-agent working directory isolation (runs/<job_id>/agent_XX layout)",
            f"srun bash -c '\n"
            f"export CODESWARM_JOB_ID=$SLURM_JOB_ID\n"
            f"export CODESWARM_NODE_ID=$SLURM_PROCID\n"
            f"export CODESWARM_BASE_DIR={hpc_base}\n"
            f"export CODESWARM_CODEX_BIN={hpc_base}/tools/npm-global/bin/codex\n"
            f"export PATH={hpc_base}/tools/npm-global/bin:$PATH\n"
            f"AGENT_INDEX=$(printf \"%02d\" $SLURM_PROCID)\n"
            f"AGENT_WORKDIR=\"{hpc_base}/runs/$SLURM_JOB_ID/agent_${{AGENT_INDEX}}\"\n"
            f"mkdir -p \"$AGENT_WORKDIR\"\n"
            f"cd \"$AGENT_WORKDIR\"\n"
            f"python3 {hpc_base}/agent/codex_worker.py\n"
            f"'",
        ])

    elif args.launch_codex_test:
        lines.extend([
            f"export WORKSPACE_ROOT={workspace_root}",
            f"export CLUSTER_SUBDIR={cluster_subdir}",
            f"export NPM_CONFIG_PREFIX={hpc_base}/tools/npm-global",
            f"export PATH={hpc_base}/tools/npm-global/bin:$PATH",
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
    login_host = resolve_login_host(config)
    workspace_root, cluster_subdir = resolve_slurm_paths(config)

    tools_dir = f"{workspace_root}/{cluster_subdir}/tools"
    node_bin = f"{tools_dir}/node/bin"
    npm_bin = f"{node_bin}/npm"
    npm_prefix = f"{tools_dir}/npm-global"
    codex_bin = f"{npm_prefix}/bin/codex"
    beads_bin = f"{npm_prefix}/bin/beads"

    # Ensure Node/npm exist in workspace
    if ssh_login(login_host, f'test -x "{npm_bin}"').returncode != 0:
        print("ERROR: Node/npm not found in workspace tools directory.")
        print(f"Expected npm at: {npm_bin}")
        sys.exit(1)

    # Ensure codex installed in workspace (version pinned)
    CODEX_VERSION = "latest"  # change to explicit version if desired, e.g. "1.2.3"

    if ssh_login(login_host, f'test -x "{codex_bin}"').returncode != 0:
        print("Codex not found in workspace. Installing...")
        install_cmd = (
            f'NPM_CONFIG_PREFIX="{npm_prefix}" '
            f'"{npm_bin}" install -g @openai/codex@{CODEX_VERSION}'
        )
        result = ssh_login(login_host, install_cmd)
        if result.returncode != 0:
            print("Codex installation failed:")
            print(result.stderr)
            sys.exit(1)

    # Optional: beads helper CLI. Launch should not fail if this install is blocked
    # by host-specific npm/nvm settings.
    BEADS_VERSION = "latest"  # change to explicit version if desired, e.g. "1.2.3"

    if ssh_login(login_host, f'test -x "{beads_bin}"').returncode != 0:
        print("beads not found in workspace. Installing (optional)...")
        install_cmd = (
            f'NPM_CONFIG_PREFIX="{npm_prefix}" '
            f'"{npm_bin}" install -g beads@{BEADS_VERSION}'
        )
        result = ssh_login(login_host, install_cmd)
        if result.returncode != 0:
            print("WARNING: beads installation failed; continuing without beads.")
            if result.stderr:
                print(result.stderr)

    # Verify installed codex version (defensive check)
    version_check = ssh_login(login_host, f'"{codex_bin}" --version')
    if version_check.returncode != 0:
        print("ERROR: Codex binary exists but failed to execute --version.")
        print(version_check.stderr)
        sys.exit(1)

    if ssh_login(login_host, f'test -x "{beads_bin}"').returncode == 0:
        beads_version_check = ssh_login(login_host, f'"{beads_bin}" --version')
        if beads_version_check.returncode != 0:
            print("WARNING: beads binary exists but failed to execute --version; continuing.")
            if beads_version_check.stderr:
                print(beads_version_check.stderr)

    # Check for existing Codex home configuration
    codex_home_check = ssh_login(
        login_host,
        'test -d "$HOME/.codex"'
    )

    if codex_home_check.returncode == 0:
        print("Existing ~/.codex detected. Skipping login.")
        return

    print("No ~/.codex directory found. Codex not authenticated.")

    # Ensure OPENAI_API_KEY exists on login node
    key_check = ssh_login(login_host, 'test -n "$OPENAI_API_KEY"')

    if key_check.returncode != 0:
        print("\nERROR:")
        print("Codex not authenticated and OPENAI_API_KEY is not set.")
        print("On the login node, run:")
        print("  export OPENAI_API_KEY=sk-...")
        print("Then re-run codeswarm.\n")
        sys.exit(1)

    print("Authenticating Codex using OPENAI_API_KEY...")
    login_result = ssh_login(
        login_host,
        f'printf "%s" "$OPENAI_API_KEY" | "{codex_bin}" login --with-api-key'
    )

    if login_result.returncode != 0:
        print("Codex login failed:")
        print(login_result.stderr)
        sys.exit(1)

    # Verify ~/.codex now exists
    verify_home = ssh_login(
        login_host,
        'test -d "$HOME/.codex"'
    )

    if verify_home.returncode != 0:
        print("Codex login did not create ~/.codex.")
        sys.exit(1)

    print("Codex authenticated successfully.")


def ensure_partition_capacity(args, config):
    login_host = resolve_login_host(config)

    slurm_cfg = config.get("cluster", {}).get("slurm", {})

    partition = (
        args.partition
        or slurm_cfg.get("default_partition")
    )

    if not partition:
        print("No partition specified and no default partition configured.")
        sys.exit(1)

    result = ssh_login(
        login_host,
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
    login_host = resolve_login_host(config)

    script = build_sbatch_script(args, config)

    print("----- SBATCH SCRIPT BEGIN -----")
    print(script)
    print("----- SBATCH SCRIPT END -----")

    result = ssh_login(login_host, "sbatch", input_text=script)

    if result.returncode != 0:
        print(result.stderr)
        sys.exit(1)

    for token in result.stdout.split():
        if token.isdigit():
            return token

    raise RuntimeError("Failed to parse job ID")


def wait_running(job_id, config):
    login_host = resolve_login_host(config)

    while True:
        result = ssh_login(login_host, f"squeue -j {job_id} -h -o %T")
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
    parser.add_argument("--agents-md-b64")
    parser.add_argument("--agents-bundle-b64")
    parser.add_argument("--launch-codex-test", action="store_true")
    parser.add_argument("--launch-codex-run", action="store_true")

    args = parser.parse_args()
    config = load_config(args.config)

    if not args.time:
        slurm_cfg = config.get("cluster", {}).get("slurm", {})
        args.time = slurm_cfg.get("time_limit") or slurm_cfg.get("default_time")

    workspace_root, cluster_subdir = resolve_slurm_paths(config)
    hpc_base = f"{workspace_root}/{cluster_subdir}"
    login_host = resolve_login_host(config)

    # ✅ DEPLOY AGENT DIRECTORY (worker + follower) BEFORE SUBMISSION
    if args.launch_codex_run:
        agent_local_dir = Path(__file__).parent.parent / "agent"
        agent_remote_dir = f"{hpc_base}/agent"

        # Ensure remote directory exists
        ssh_login(login_host, f"mkdir -p {agent_remote_dir}")

        # Rsync entire agent directory (codex_worker.py, outbox_follower.py, future utilities)
        subprocess.run(
            [
                "rsync",
                "-az",
                str(agent_local_dir) + "/",
                f"{login_host}:{agent_remote_dir}/",
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
        ssh_login(login_host, f"mkdir -p {run_base}")
        agents_md_content = None
        agents_bundle = None
        if args.agents_md_b64:
            try:
                agents_md_content = base64.b64decode(args.agents_md_b64).decode("utf-8")
            except Exception as e:
                raise RuntimeError(f"Invalid --agents-md-b64 payload: {e}") from e
        if args.agents_bundle_b64:
            try:
                decoded = base64.b64decode(args.agents_bundle_b64).decode("utf-8")
                parsed = json.loads(decoded)
                if isinstance(parsed, dict):
                    agents_bundle = parsed
            except Exception as e:
                raise RuntimeError(f"Invalid --agents-bundle-b64 payload: {e}") from e

        bundle_md = agents_bundle.get("agents_md_content") if isinstance(agents_bundle, dict) else None
        if isinstance(bundle_md, str) and bundle_md.strip():
            agents_md_content = bundle_md
        bundle_mode = str(agents_bundle.get("mode") or "file") if isinstance(agents_bundle, dict) else "file"
        raw_skills = agents_bundle.get("skills_files") if isinstance(agents_bundle, dict) else []
        skill_files = []
        if bundle_mode == "directory" and isinstance(raw_skills, list):
            for item in raw_skills:
                if not isinstance(item, dict):
                    continue
                rel_path = item.get("path")
                content = item.get("content")
                if not isinstance(rel_path, str) or not isinstance(content, str):
                    continue
                safe_rel = safe_skill_rel_path(rel_path)
                if not safe_rel:
                    continue
                skill_files.append((safe_rel, content))

        for i in range(args.nodes):
            agent_dir = f"{run_base}/agent_{i:02d}"
            ssh_login(login_host, f"mkdir -p {agent_dir}")
            ssh_login(
                login_host,
                f'echo "Agent {i}: Say hello in one short sentence." > {agent_dir}/PROMPT.txt'
            )
            if agents_md_content is not None:
                ssh_login(
                    login_host,
                    f"cat > {shlex.quote(agent_dir + '/AGENTS.md')}",
                    input_text=agents_md_content,
                )
            for rel_path, content in skill_files:
                remote_file = f"{agent_dir}/.agents/skills/{rel_path}"
                remote_dir = str(PurePosixPath(remote_file).parent)
                ssh_login(login_host, f"mkdir -p {shlex.quote(remote_dir)}")
                ssh_login(
                    login_host,
                    f"cat > {shlex.quote(remote_file)}",
                    input_text=content,
                )

    print("Waiting for RUNNING state...")
    wait_running(job_id, config)

    print("Allocation complete.")
    print(f"JOB_ID={job_id}")
