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
    msg = params.get("msg", {})

    # --- Turn started ---
    if method == "turn/started":
        return {
            "type": "turn_started",
            "job_id": event["job_id"],
            "node_id": event["node_id"]
        }

    # --- Streaming assistant deltas ---
    if method == "codex/event/agent_message_content_delta":
        delta = msg.get("delta")
        if delta:
            return {
                "type": "assistant_delta",
                "job_id": event["job_id"],
                "node_id": event["node_id"],
                "content": delta
            }

    # --- Final assistant message ---
    if method == "codex/event/agent_message":
        message = msg.get("message")
        if message is not None:
            return {
                "type": "assistant",
                "job_id": event["job_id"],
                "node_id": event["node_id"],
                "content": message
            }

    # --- Token usage ---
    if method == "codex/event/token_count":
        info = msg.get("info") or {}
        total = info.get("total_token_usage", {}).get("total_tokens")
        if total is not None:
            return {
                "type": "usage",
                "job_id": event["job_id"],
                "node_id": event["node_id"],
                "total_tokens": total
            }

    # --- Turn complete (agent message item completed) ---
    if method == "item/completed":
        item = params.get("item", {})
        if item.get("type") == "agentMessage":
            return {
                "type": "turn_complete",
                "job_id": event["job_id"],
                "node_id": event["node_id"]
            }

    return None


def stream_outbox(config, fixed_job_id=None):
    login_alias = config["ssh"]["login_alias"]
    workspace_root = config["cluster"]["workspace_root"]
    cluster_subdir = config["cluster"]["cluster_subdir"]

    outbox_dir = f"{workspace_root}/{cluster_subdir}/mailbox/outbox"

    print(f"Streaming outbox for active Slurm jobs in: {outbox_dir}")

    import select

    while True:
        # --- Determine which jobs to monitor ---
        if fixed_job_id:
            active_jobs = [fixed_job_id]
        else:
            result = subprocess.run(
                ["ssh", login_alias, "squeue -h -n codeswarm -o %A"],
                capture_output=True,
                text=True
            )

            if result.returncode != 0:
                print("Failed to query active Slurm jobs.")
                print(result.stderr)
                time.sleep(5)
                continue

            active_jobs = [jid.strip() for jid in result.stdout.splitlines() if jid.strip()]

            if not active_jobs:
                print("No active Slurm jobs found. Retrying in 5 seconds...")
                time.sleep(5)
                continue

        print("Monitoring jobs:", active_jobs)

        # For now assume single-node per job (_00)
        outbox_files = [
            f"{outbox_dir}/{jid}_00.jsonl"
            for jid in active_jobs
        ]

        # Build SSH tail command (explicit filenames, no glob)
        cmd = [
            "ssh",
            login_alias,
            "stdbuf",
            "-oL",
            "-eL",
            "tail",
            "-n",
            "+1",
            "-F",
        ] + outbox_files

        print("DEBUG SSH CMD:", cmd)

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0
        )

        stdout_buffer = b""
        stderr_buffer = b""

        try:
            while True:
                ready, _, _ = select.select([proc.stdout, proc.stderr], [], [], 0.5)

                for stream in ready:
                    chunk = stream.read(4096)
                    if not chunk:
                        continue

                    if stream is proc.stderr:
                        stderr_buffer += chunk
                        while b"\n" in stderr_buffer:
                            line, stderr_buffer = stderr_buffer.split(b"\n", 1)
                            line = line.decode("utf-8", errors="replace").strip()
                            if line:
                                print("SSH STDERR:", line)
                        continue

                    # stdout handling
                    stdout_buffer += chunk

                    while b"\n" in stdout_buffer:
                        line, stdout_buffer = stdout_buffer.split(b"\n", 1)
                        line = line.decode("utf-8", errors="replace").strip()

                        if not line:
                            continue

                        print(f"RAW: {line}")

                        # Ignore tail headers like ==> file <==
                        if line.startswith("==>") and line.endswith("<=="):
                            continue

                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            print("Could not decode " + line)
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
    parser.add_argument("--job-id", help="Monitor a specific job_id without querying Slurm")

    args = parser.parse_args()
    config = load_config(args.config)

    if args.stream_outbox:
        stream_outbox(config, fixed_job_id=args.job_id)
