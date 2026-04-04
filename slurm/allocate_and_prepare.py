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


def _truthy_flag(value) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def launch_worker_requested(args) -> bool:
    return bool(getattr(args, "launch_worker_run", False) or getattr(args, "launch_codex_run", False))


def resolved_worker_mode(args) -> str:
    mode = str(getattr(args, "worker_mode", "") or "").strip().lower()
    if mode:
        return mode
    if getattr(args, "launch_codex_run", False):
        return "codex"
    return "codex"


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

    if launch_worker_requested(args):
        worker_mode = resolved_worker_mode(args)
        capture_all_session = str(os.environ.get("CODESWARM_CAPTURE_ALL_SESSION") or "").strip()
        capture_line = (
            f"export CODESWARM_CAPTURE_ALL_SESSION={shlex.quote(capture_all_session)}\n"
            if capture_all_session
            else ""
        )
        worker_lines = [
            "export CODESWARM_JOB_ID=$SLURM_JOB_ID",
            "export CODESWARM_NODE_ID=$SLURM_PROCID",
            f"export CODESWARM_BASE_DIR={hpc_base}",
            f"export CODESWARM_ASK_FOR_APPROVAL={shlex.quote(str(args.approval_policy or 'never'))}",
        ]
        if getattr(args, "fresh_thread_per_injection", None) is not None:
            worker_lines.append(
                "export CODESWARM_FRESH_THREAD_PER_INJECTION="
                + shlex.quote("1" if _truthy_flag(args.fresh_thread_per_injection) else "0")
            )
        if worker_mode == "codex":
            worker_lines.extend(
                [
                    f"export CODESWARM_CODEX_BIN={hpc_base}/tools/npm-global/bin/codex",
                    f"export PATH={hpc_base}/tools/npm-global/bin:$PATH",
                ]
            )
            if capture_line:
                worker_lines.append(capture_line.rstrip("\n"))
            worker_entrypoint = f"python3 {hpc_base}/agent/codex_worker.py"
        elif worker_mode == "claude":
            worker_lines.append(
                "export CODESWARM_CLAUDE_PERMISSION_MODE="
                + shlex.quote(str(args.claude_permission_mode or "bypassPermissions"))
            )
            worker_lines.extend(
                [
                    f'CLAUDE_VENV="{hpc_base}/tools/claude-venv"',
                    'if [ ! -x "$CLAUDE_VENV/bin/python" ]; then',
                    '  echo "Claude runtime virtualenv is missing at $CLAUDE_VENV" >&2',
                    "  exit 1",
                    "fi",
                    'export PATH="$CLAUDE_VENV/bin:$PATH"',
                ]
            )
            if args.claude_env_file:
                quoted_env_file = shlex.quote(str(args.claude_env_file))
                worker_lines.extend(
                    [
                        f"if [ ! -f {quoted_env_file} ]; then",
                        f'  echo "Claude env file is missing at {args.claude_env_file}" >&2',
                        "  exit 1",
                        "fi",
                        f". {quoted_env_file}",
                    ]
                )
            if args.claude_model:
                worker_lines.append("export CODESWARM_CLAUDE_MODEL=" + shlex.quote(str(args.claude_model)))
            if args.claude_cli_path:
                worker_lines.append("export CODESWARM_CLAUDE_CLI_PATH=" + shlex.quote(str(args.claude_cli_path)))
            worker_entrypoint = '"$CLAUDE_VENV/bin/python" ' + f'"{hpc_base}/agent/claude_worker.py"'
        else:
            raise RuntimeError(f"Unsupported worker_mode: {worker_mode}")
        worker_lines.extend(
            [
                'AGENT_INDEX=$(printf "%02d" $SLURM_PROCID)',
                f'AGENT_WORKDIR="{hpc_base}/runs/$SLURM_JOB_ID/agent_${{AGENT_INDEX}}"',
                'mkdir -p "$AGENT_WORKDIR"',
                'cd "$AGENT_WORKDIR"',
                worker_entrypoint,
            ]
        )
        lines.extend(
            [
                "",
                "# Per-agent working directory isolation (runs/<job_id>/agent_XX layout)",
                "srun bash -c '\n" + "\n".join(worker_lines) + "\n'",
            ]
        )

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
    beads_bin = f"{npm_prefix}/bin/bd"
    legacy_beads_bin = f"{npm_prefix}/bin/beads"

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

    beads_present = ssh_login(
        login_host,
        f'test -x "{beads_bin}" || test -x "{legacy_beads_bin}"',
    ).returncode == 0
    if not beads_present:
        print("Beads CLI (bd) not found in workspace. Installing (optional)...")
        install_cmd = (
            f'NPM_CONFIG_PREFIX="{npm_prefix}" '
            f'"{npm_bin}" install -g @beads/bd@{BEADS_VERSION}'
        )
        result = ssh_login(login_host, install_cmd)
        if result.returncode != 0:
            print("WARNING: Beads CLI installation failed; continuing without bd/beads.")
            if result.stderr:
                print(result.stderr)

    # Verify installed codex version (defensive check)
    version_check = ssh_login(login_host, f'"{codex_bin}" --version')
    if version_check.returncode != 0:
        print("ERROR: Codex binary exists but failed to execute --version.")
        print(version_check.stderr)
        sys.exit(1)

    beads_version_check = ssh_login(
        login_host,
        (
            f'if test -x "{beads_bin}"; then "{beads_bin}" --version; '
            f'elif test -x "{legacy_beads_bin}"; then "{legacy_beads_bin}" --version; '
            f'else exit 0; fi'
        ),
    )
    if beads_version_check.returncode != 0:
        print("WARNING: Beads CLI binary exists but failed to execute --version; continuing.")
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


def ensure_claude_ready(config):
    login_host = resolve_login_host(config)
    workspace_root, cluster_subdir = resolve_slurm_paths(config)
    slurm_cfg = config.get("cluster", {}).get("slurm", {})

    tools_dir = f"{workspace_root}/{cluster_subdir}/tools"
    claude_venv = f"{tools_dir}/claude-venv"
    claude_sdk_package = str(slurm_cfg.get("claude_sdk_package") or "claude-agent-sdk").strip() or "claude-agent-sdk"

    bootstrap_script = f"""
set -euo pipefail
TOOLS_DIR={shlex.quote(tools_dir)}
CLAUDE_VENV={shlex.quote(claude_venv)}

mkdir -p "$TOOLS_DIR"

if [ ! -x "$CLAUDE_VENV/bin/python" ]; then
  python3 -m venv "$CLAUDE_VENV"
fi

"$CLAUDE_VENV/bin/python" -m pip install --upgrade pip setuptools wheel
"$CLAUDE_VENV/bin/python" -m pip install {shlex.quote(claude_sdk_package)}
"$CLAUDE_VENV/bin/python" -c "import claude_agent_sdk"
"""
    result = ssh_login(login_host, bootstrap_script)
    if result.returncode != 0:
        print("Claude runtime bootstrap failed:")
        if result.stderr:
            print(result.stderr)
        if result.stdout:
            print(result.stdout)
        sys.exit(1)


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
    parser.add_argument("--launch-worker-run", action="store_true")
    parser.add_argument("--worker-mode")
    parser.add_argument("--approval-policy")
    parser.add_argument("--fresh-thread-per-injection")
    parser.add_argument("--claude-model")
    parser.add_argument("--claude-cli-path")
    parser.add_argument("--claude-permission-mode")
    parser.add_argument("--claude-env-file")
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
    if launch_worker_requested(args):
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
    if launch_worker_requested(args):
        worker_mode = resolved_worker_mode(args)
        if worker_mode == "codex":
            ensure_codex_ready(config)
        elif worker_mode == "claude":
            ensure_claude_ready(config)

    # Ensure partition has sufficient idle nodes
    ensure_partition_capacity(args, config)

    print("Submitting allocation job...")
    job_id = submit_job(args, config)
    print(f"Submitted job {job_id}")

    # ✅ CREATE RUN DIRECTORIES AFTER JOB ID EXISTS
    if launch_worker_requested(args):
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
