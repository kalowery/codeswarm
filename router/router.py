#!/usr/bin/env python3
import argparse
import subprocess
import json
import time
from pathlib import Path
import sys

sys.path.append(str(Path(__file__).resolve().parents[1]))
from common.config import load_config


def ssh(login_alias, cmd):
    return subprocess.run(["ssh", login_alias, cmd], capture_output=True, text=True)


def translate_event(event):
    # Only process structured RPC events from worker
    if event.get("type") != "codex_rpc":
        return None

    payload = event.get("payload", {})
    method = payload.get("method")
    params = payload.get("params", {})

    # --- Streaming assistant deltas ---
    if method == "codex/event/agent_message_content_delta":
        delta = params.get("msg", {}).get("delta")
        if delta:
            return {
                "type": "assistant_delta",
                "job_id": event["job_id"],
                "node_id": event["node_id"],
                "content": delta
            }

    # --- Final assistant message ---
    if method == "codex/event/agent_message":
        message = params.get("msg", {}).get("message")
        if message:
            return {
                "type": "assistant",
                "job_id": event["job_id"],
                "node_id": event["node_id"],
                "content": message
            }

    # --- Token usage ---
    if method == "codex/event/token_count":
        info = params.get("msg", {}).get("info", {})
        total = info.get("total_token_usage", {}).get("total_tokens")
        if total is not None:
            return {
                "type": "usage",
                "job_id": event["job_id"],
                "node_id": event["node_id"],
                "total_tokens": total
            }

    # --- Turn started ---
    if method == "turn/started":
        return {
            "type": "turn_started",
            "job_id": event["job_id"],
            "node_id": event["node_id"]
        }

    # --- Turn complete ---
    if method == "item/completed":
        item = params.get("item", {})
        if item.get("type") == "agentMessage":
            return {
                "type": "turn_complete",
                "job_id": event["job_id"],
                "node_id": event["node_id"]
            }

    return None


def stream_outbox(config):
    login_alias = config["ssh"]["login_alias"]
    workspace_root = config["cluster"]["workspace_root"]
    cluster_subdir = config["cluster"]["cluster_subdir"]

    outbox_dir = f"{workspace_root}/{cluster_subdir}/mailbox/outbox"

    print(f"Streaming outbox for active Slurm jobs in: {outbox_dir}")

    # --- Query active Slurm job IDs ---
    result = subprocess.run(
        ["ssh", login_alias, "squeue -h -n codeswarm -o %A"],
        capture_output=True,
        text=True
    )

    if result.returncode != 0:
        print("Failed to query active Slurm jobs.")
        print(result.stderr)
        return

    active_jobs = [jid.strip() for jid in result.stdout.splitlines() if jid.strip()]

    if not active_jobs:
        print("No active Slurm jobs found.")
        return

    print("Active jobs:", active_jobs)

    # For now assume single-node per job (_00)
    outbox_files = [
        f"{outbox_dir}/{jid}_00.jsonl"
        for jid in active_jobs
    ]

    # Build SSH tail command (explicit filenames, no glob)
    cmd = [
        "ssh",
        login_alias,
        "tail",
        "-n",
        "0",
        "-F",
    ] + outbox_files

    print("DEBUG SSH CMD:", cmd)

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1
    )

    import select

    try:
        while True:
            ready, _, _ = select.select([proc.stdout, proc.stderr], [], [], 0.5)

            for stream in ready:
                line = stream.readline()
                if not line:
                    continue

                line = line.strip()
                if not line:
                    continue

                # STDERR
                if stream is proc.stderr:
                    print("SSH STDERR:", line)
                    continue

                # Ignore tail headers like ==> file <==
                if line.startswith("==>") and line.endswith("<=="):
                    continue

                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                translated = translate_event(event)
                if translated:
                    print(json.dumps(translated, indent=2))

            if proc.poll() is not None:
                print("SSH stream terminated.")
                break

    except KeyboardInterrupt:
        print("Stopping stream...")
    finally:
        proc.terminate()

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--stream-outbox", action="store_true")

    args = parser.parse_args()
    config = load_config(args.config)

    if args.stream_outbox:
        stream_outbox(config)
