#!/usr/bin/env python3
import argparse
import subprocess
import json
import time
import shlex
import uuid
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

    injection_id = event.get("injection_id")

    # --- Turn started ---
    if method == "turn/started":
        return {
            "type": "turn_started",
            "job_id": event["job_id"],
            "node_id": event["node_id"],
            "injection_id": injection_id
        }

    # --- Streaming assistant deltas ---
    if method == "codex/event/agent_message_content_delta":
        delta = msg.get("delta")
        if delta:
            return {
                "type": "assistant_delta",
                "job_id": event["job_id"],
                "node_id": event["node_id"],
                "injection_id": injection_id,
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
                "injection_id": injection_id,
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
                "injection_id": injection_id,
                "total_tokens": total
            }

    # --- Turn complete (agent message item completed) ---
    if method == "item/completed":
        item = params.get("item", {})
        if item.get("type") == "agentMessage":
            return {
                "type": "turn_complete",
                "job_id": event["job_id"],
                "node_id": event["node_id"],
                "injection_id": injection_id
            }

    return None


def stream_outbox(config, fixed_job_id=None):
    login_alias = config["ssh"]["login_alias"]
    workspace_root = config["cluster"]["workspace_root"]
    cluster_subdir = config["cluster"]["cluster_subdir"]

    outbox_dir = f"{workspace_root}/{cluster_subdir}/mailbox/outbox"

    print(f"Streaming outbox via remote follower in: {outbox_dir}")

    agent_remote_dir = f"{workspace_root}/{cluster_subdir}/agent"
    follower_remote_path = f"{agent_remote_dir}/outbox_follower.py"

    # Ensure follower exists on remote (control-plane bootstrap)
    check = subprocess.run([
        "ssh", login_alias, f"test -f {follower_remote_path}"
    ])

    if check.returncode != 0:
        print("Remote outbox_follower.py not found. Deploying...")
        local_follower = Path(__file__).resolve().parents[1] / "agent" / "outbox_follower.py"
        subprocess.run([
            "ssh", login_alias, f"mkdir -p {agent_remote_dir}"
        ], check=True)
        subprocess.run([
            "rsync",
            "-az",
            str(local_follower),
            f"{login_alias}:{agent_remote_dir}/"
        ], check=True)
        print("Deployment complete.")

    cmd = [
        "ssh",
        login_alias,
        "python3",
        follower_remote_path,
        outbox_dir
    ]

    print("DEBUG SSH CMD:", cmd)

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0
    )

    stdout_buffer = b""
    stderr_buffer = b""

    import select

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

                stdout_buffer += chunk

                while b"\n" in stdout_buffer:
                    line, stdout_buffer = stdout_buffer.split(b"\n", 1)
                    line = line.decode("utf-8", errors="replace").strip()

                    if not line:
                        continue

                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        print("Could not decode " + line)
                        continue

                    if fixed_job_id and event.get("job_id") != fixed_job_id:
                        continue

                    translated = translate_event(event)
                    if translated:
                        print(json.dumps(translated, indent=2))

            if proc.poll() is not None:
                print("Remote follower terminated.")
                break

    except KeyboardInterrupt:
        print("Router shutting down.")
        proc.terminate()

def inject_prompt(config, job_id, node_id, text):
    login_alias = config["ssh"]["login_alias"]
    workspace_root = config["cluster"]["workspace_root"]
    cluster_subdir = config["cluster"]["cluster_subdir"]

    inbox_path = f"{workspace_root}/{cluster_subdir}/mailbox/inbox/{job_id}_{int(node_id):02d}.jsonl"

    injection_id = str(uuid.uuid4())

    payload = {
        "type": "user",
        "content": text,
        "injection_id": injection_id
    }

    json_line = json.dumps(payload)
    remote_cmd = f"printf '%s\n' {shlex.quote(json_line)} >> {inbox_path}"

    result = subprocess.run(["ssh", login_alias, remote_cmd])

    if result.returncode != 0:
        print("Injection failed.")
        sys.exit(1)

    print(f"Injected prompt to job {job_id} node {int(node_id):02d}.")
    print(f"injection_id={injection_id}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--stream-outbox", action="store_true")
    parser.add_argument("--job-id", help="Monitor a specific job_id without querying Slurm")
    parser.add_argument("--inject", action="store_true", help="Inject a prompt into a running job")
    parser.add_argument("--node", default=0, help="Node id for injection (default 0)")
    parser.add_argument("--text", help="Prompt text for injection")

    args = parser.parse_args()
    config = load_config(args.config)

    if args.inject:
        if not args.job_id or not args.text:
            print("--inject requires --job-id and --text")
            sys.exit(1)
        inject_prompt(config, args.job_id, args.node, args.text)
        sys.exit(0)

    if args.stream_outbox:
        stream_outbox(config, fixed_job_id=args.job_id)
