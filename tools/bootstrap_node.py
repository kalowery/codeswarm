#!/usr/bin/env python3
import sys
import argparse
import subprocess
from pathlib import Path

# Make project root importable
sys.path.append(str(Path(__file__).resolve().parents[1]))

from common.config import load_config

NODE_VERSION = "v24.14.0"
NODE_DIST = f"node-{NODE_VERSION}-linux-x64"
NODE_TARBALL = f"{NODE_DIST}.tar.xz"
NODE_URL = f"https://nodejs.org/dist/{NODE_VERSION}/{NODE_TARBALL}"


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
    node_dir = f"{tools_dir}/node"
    node_bin = f"{node_dir}/bin/node"

    print("Checking for existing Node installation...")
    check = ssh_cmd(login_alias, f"[ -x {node_bin} ] && echo FOUND || echo MISSING")

    if "FOUND" in check.stdout:
        print("Node already installed.")
    else:
        print("Node not found. Installing...")

        install_script = f"""
        set -e
        mkdir -p {tools_dir}
        cd {tools_dir}
        curl -LO {NODE_URL}
        tar -xf {NODE_TARBALL}
        mv {NODE_DIST} node
        rm -f {NODE_TARBALL}
        """

        result = ssh_cmd(login_alias, install_script)
        if result.returncode != 0:
            print("Node installation failed:")
            print(result.stderr)
            sys.exit(1)

        print("Node installed successfully.")

    print("Verifying Node version...")
    version_check = ssh_cmd(login_alias, f"{node_bin} --version")
    if version_check.returncode != 0:
        print("Failed to verify Node installation:")
        print(version_check.stderr)
        sys.exit(1)

    print("Node version:", version_check.stdout.strip())


if __name__ == "__main__":
    main()
