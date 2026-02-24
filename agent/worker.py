#!/usr/bin/env python3
import os
import time
import json
import socket
from pathlib import Path

job_id = os.environ["SLURM_JOB_ID"]
proc_id = int(os.environ.get("SLURM_PROCID", "0")) + 1
hostname = socket.gethostname()

workspace_root = os.environ["WORKSPACE_ROOT"]
cluster_subdir = os.environ["CLUSTER_SUBDIR"]

base_dir = Path(workspace_root) / cluster_subdir
job_dir = base_dir / f"job_{job_id}"
node_dir = job_dir / f"node_{proc_id:02d}_{hostname}"

node_dir.mkdir(parents=True, exist_ok=True)

heartbeat_path = node_dir / "heartbeat.json"
outbox_path = node_dir / "outbox.jsonl"

print(f"Worker started on {hostname}", flush=True)

while True:
    heartbeat_path.write_text(json.dumps({
        "timestamp": time.time(),
        "hostname": hostname
    }))

    with open(outbox_path, "a") as f:
        f.write(json.dumps({
            "timestamp": time.time(),
            "message": f"alive from {hostname}"
        }) + "\n")

    time.sleep(5)
