#!/usr/bin/env python3
import sys
import shutil
import datetime
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))
from common.config import load_config


def parse_login_block(login_alias):
    config_path = Path.home() / ".ssh" / "config"
    lines = config_path.read_text().splitlines()

    in_block = False
    user = None
    identity_files = []

    for line in lines:
        stripped = line.strip()

        if stripped.lower().startswith("host "):
            hosts = stripped.split()[1:]
            if login_alias in hosts:
                in_block = True
                continue
            else:
                if in_block:
                    break
                continue

        if in_block:
            parts = stripped.split(None, 1)
            if len(parts) != 2:
                continue

            key, value = parts[0].lower(), parts[1].strip()
            if key == "user":
                user = value
            elif key == "identityfile":
                identity_files.append(value)

    if not user or not identity_files:
        raise RuntimeError(f"Incomplete SSH block for {login_alias}")

    return user, identity_files


def main():
    config_path_arg = sys.argv[1]
    job_id = sys.argv[2]
    node_map = dict(arg.split("=") for arg in sys.argv[3:])

    config = load_config(config_path_arg)
    login_alias = config["ssh"]["login_alias"]
    control_minutes = config["ssh"]["controlpersist_minutes"]

    user, identity_files = parse_login_block(login_alias)

    ssh_dir = Path.home() / ".ssh"
    ssh_dir.mkdir(exist_ok=True)

    (ssh_dir / "controlmasters").mkdir(exist_ok=True)

    config_path = ssh_dir / "config"
    backup_path = ssh_dir / f"config.codex_backup_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}"
    shutil.copy2(config_path, backup_path)

    content = config_path.read_text()
    start_marker = "# >>> CODEX_CLUSTER_MANAGED_START"
    end_marker = "# <<< CODEX_CLUSTER_MANAGED_END"

    if start_marker in content and end_marker in content:
        pre = content.split(start_marker)[0]
        post = content.split(end_marker)[1]
    else:
        pre = content
        post = ""

    block = [start_marker, f"# JOB {job_id} START"]

    for idx, (hostname, ip) in enumerate(sorted(node_map.items()), start=1):
        alias = f"codex-{job_id}-{idx:02d}"
        block.append(f"Host {alias}")
        block.append(f"    HostName {ip}")
        block.append(f"    User {user}")
        for ident in identity_files:
            block.append(f"    IdentityFile {ident}")
        block.extend([
            "    IdentitiesOnly yes",
            f"    ProxyJump {login_alias}",
            "    UserKnownHostsFile ~/.ssh/known_hosts_codex",
            "    StrictHostKeyChecking accept-new",
            "    ControlMaster auto",
            f"    ControlPersist {control_minutes}m",
            "    ControlPath ~/.ssh/controlmasters/%r@%h:%p",
            "    ServerAliveInterval 30",
            "    ServerAliveCountMax 3",
            ""
        ])

    block.extend([f"# JOB {job_id} END", end_marker])

    new_content = pre.strip() + "\n\n" + "\n".join(block) + "\n" + post
    config_path.write_text(new_content)

    print(f"SSH config updated for job {job_id}")


if __name__ == "__main__":
    main()
