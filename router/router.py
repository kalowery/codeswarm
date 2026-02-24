#!/usr/bin/env python3
import sys
import time
import argparse
import subprocess
from pathlib import Path

# Make project root importable
sys.path.append(str(Path(__file__).resolve().parents[1]))

from common.config import load_config


def ssh_cmd(host, cmd):
    return subprocess.run(
        ["ssh", host, cmd],
        capture_output=True,
        text=True,
    )


def find_job_aliases(job_id):
    config_path = Path.home() / ".ssh" / "config"
    if not config_path.exists():
        raise RuntimeError("~/.ssh/config not found")

    aliases = []
    for line in config_path.read_text().splitlines():
        if line.startswith(f"Host codex-{job_id}-"):
            aliases.append(line.split()[1])

    return aliases


def monitor_job(config, job_id):
    workspace_root = config["cluster"]["workspace_root"]
    cluster_subdir = config["cluster"]["cluster_subdir"]

    base_path = f"{workspace_root}/{cluster_subdir}"
    aliases = find_job_aliases(job_id)

    if not aliases:
        print("No aliases found for job.")
        sys.exit(1)

    print(f"Monitoring job {job_id} (Ctrl+C to stop)...")

    while True:
        for alias in aliases:
            node_dir_cmd = (
                f"ls -d {base_path}/job_{job_id}/node_* 2>/dev/null | head -n1"
            )
            result = ssh_cmd(alias, node_dir_cmd)
            node_dir = result.stdout.strip()

            if not node_dir:
                print(f"[{alias}] No node directory found yet.")
                continue

            hb = ssh_cmd(alias, f"cat {node_dir}/heartbeat.json 2>/dev/null")
            out = ssh_cmd(alias, f"tail -n 3 {node_dir}/outbox.jsonl 2>/dev/null")

            print("\n----------------------------------------")
            print(f"[{alias}]")
            print("Heartbeat:")
            print(hb.stdout.strip() or "(no heartbeat)")
            print("Recent logs:")
            print(out.stdout.strip() or "(no logs)")

        time.sleep(5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--monitor-job")
    args = parser.parse_args()

    config = load_config(args.config)

    if args.monitor_job:
        monitor_job(config, args.monitor_job)
