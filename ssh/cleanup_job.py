#!/usr/bin/env python3
import sys
from pathlib import Path

job_id = sys.argv[1]
config_path = Path.home() / ".ssh" / "config"

if not config_path.exists():
    print("No SSH config found.")
    sys.exit(0)

content = config_path.read_text()
start_marker = "# >>> CODEX_CLUSTER_MANAGED_START"
end_marker = "# <<< CODEX_CLUSTER_MANAGED_END"

if start_marker not in content or end_marker not in content:
    print("No managed block found.")
    sys.exit(0)

pre = content.split(start_marker)[0]
man_block = content.split(start_marker)[1].split(end_marker)[0]
post = content.split(end_marker)[1]

new_lines = []
skip = False

for line in man_block.splitlines():
    if line.strip() == f"# JOB {job_id} START":
        skip = True
        continue
    if line.strip() == f"# JOB {job_id} END":
        skip = False
        continue
    if not skip:
        new_lines.append(line)

new_man_block = start_marker + "\n" + "\n".join(new_lines).strip() + "\n" + end_marker

config_path.write_text(pre + new_man_block + post)
print(f"Removed SSH config entries for job {job_id}")
