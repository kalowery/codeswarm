#!/usr/bin/env python3
import sys
import argparse
import subprocess
from pathlib import Path

# Make project root importable
sys.path.append(str(Path(__file__).resolve().parents[1]))

from common.config import load_config


def ssh_cmd(login_alias, cmd):
    return subprocess.run(
        ["ssh", login_alias, cmd],
        capture_output=True,
        text=True,
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()

    config = load_config(args.config)

    login_alias = config["ssh"]["login_alias"]
    workspace_root = config["cluster"]["workspace_root"]
    cluster_subdir = config["cluster"]["cluster_subdir"]

    tools_dir = f"{workspace_root}/{cluster_subdir}/tools"
    node_bin = f"{tools_dir}/node/bin"
    npm_prefix = f"{tools_dir}/npm-global"
    npm_bin = f"{node_bin}/npm"

    print("Ensuring npm global prefix directory exists...")
    mkdir_cmd = f"mkdir -p {npm_prefix}/bin {npm_prefix}/lib"
    result = ssh_cmd(login_alias, mkdir_cmd)
    if result.returncode != 0:
        print("Failed to create npm global directory:")
        print(result.stderr)
        sys.exit(1)

    print("Verifying npm global prefix using environment override...")
    verify_cmd = (
        f'NPM_CONFIG_PREFIX="{npm_prefix}" '
        f'"{npm_bin}" config get prefix'
    )

    result = ssh_cmd(login_alias, verify_cmd)
    if result.returncode != 0:
        print("Failed to verify npm prefix:")
        print(result.stderr)
        sys.exit(1)

    prefix_value = result.stdout.strip()
    print("npm prefix:", prefix_value)

    if prefix_value != npm_prefix:
        print("Prefix mismatch — environment override not working.")
        sys.exit(1)

    print("npm global prefix configured successfully (environment-based).")


if __name__ == "__main__":
    main()
