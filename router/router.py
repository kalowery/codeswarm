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

    print(f"Streaming outbox from {outbox_dir}...")

    known_offsets = {}

    while True:
        ls = ssh(login_alias, f"ls {outbox_dir} 2>/dev/null || true")
        files = [f.strip() for f in ls.stdout.splitlines() if f.strip()]

        for filename in files:
            full_path = f"{outbox_dir}/{filename}"

            if filename not in known_offsets:
                known_offsets[filename] = 0

            cat = ssh(login_alias, f"cat {full_path}")
            lines = [l for l in cat.stdout.splitlines() if l.strip()]

            already = known_offsets[filename]
            new_lines = lines[already:]

            for line in new_lines:
                try:
                    event = json.loads(line)
                except:
                    continue

                translated = translate_event(event)
                if translated:
                    print(json.dumps(translated, indent=2))

            known_offsets[filename] += len(new_lines)

        time.sleep(0.2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--stream-outbox", action="store_true")

    args = parser.parse_args()
    config = load_config(args.config)

    if args.stream_outbox:
        stream_outbox(config)
