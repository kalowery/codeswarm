#!/usr/bin/env python3
import argparse
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import sys

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


def transport_test(config, job_id):
    login_alias = config["ssh"]["login_alias"]
    workspace_root = config["cluster"]["workspace_root"]
    cluster_subdir = config["cluster"]["cluster_subdir"]
    max_workers = config["router"]["ssh_max_workers"]

    base_path = f"{workspace_root}/{cluster_subdir}"
    test_dir = f"{base_path}/test_{job_id}"

    aliases = find_job_aliases(job_id)

    if not aliases:
        print("No aliases found for job.")
        sys.exit(1)

    print(f"Testing {len(aliases)} nodes...")

    def test_node(alias):
        cmd = (
            f"mkdir -p {test_dir} && "
            f"echo $(hostname) > {test_dir}/transport.txt && "
            f"cat {test_dir}/transport.txt"
        )
        result = ssh_cmd(alias, cmd)

        if result.returncode == 0:
            return alias, True, result.stdout.strip()
        else:
            return alias, False, result.stderr.strip()

    successes = 0

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(test_node, a) for a in aliases]

        for future in as_completed(futures):
            alias, ok, output = future.result()

            if ok:
                print(f"[OK] {alias} -> {output}")
                successes += 1
            else:
                print(f"[FAIL] {alias} -> {output}")

    print(f"Transport Test: {successes}/{len(aliases)} nodes OK")

    if successes == len(aliases):
        print("All nodes OK. Cancelling job...")

        # Cancel job via login alias
        subprocess.run(
            ["ssh", login_alias, f"scancel {job_id}"],
            check=False
        )

        # Cleanup SSH config block
        subprocess.run([
            "python3",
            str(Path(__file__).parent.parent / "ssh" / "cleanup_job.py"),
            job_id
        ], check=False)

        print("Cleanup complete.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--transport-test")
    args = parser.parse_args()

    config = load_config(args.config)

    if args.transport_test:
        transport_test(config, args.transport_test)
