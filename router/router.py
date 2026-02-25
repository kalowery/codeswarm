#!/usr/bin/env python3
import argparse
import subprocess
import json
import time
import shlex
import uuid
import sys
import signal
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))
from common.config import load_config

DEBUG = False

def debug(msg):
    if DEBUG:
        print(f"[DEBUG] {msg}", file=sys.stderr, flush=True)


# =========================
# Injection
# =========================

def inject_prompt_internal(config, job_id, node_id, text):
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
        raise RuntimeError("Injection failed")

    return injection_id


# =========================
# Remote Follower Startup
# =========================

def start_remote_follower(config):
    login_alias = config["ssh"]["login_alias"]
    workspace_root = config["cluster"]["workspace_root"]
    cluster_subdir = config["cluster"]["cluster_subdir"]

    outbox_dir = f"{workspace_root}/{cluster_subdir}/mailbox/outbox"
    agent_remote_dir = f"{workspace_root}/{cluster_subdir}/agent"
    follower_remote_path = f"{agent_remote_dir}/outbox_follower.py"

    # Bootstrap follower if missing
    check = subprocess.run(["ssh", login_alias, f"test -f {follower_remote_path}"])

    if check.returncode != 0:
        local_follower = Path(__file__).resolve().parents[1] / "agent" / "outbox_follower.py"
        subprocess.run(["ssh", login_alias, f"mkdir -p {agent_remote_dir}"], check=True)
        subprocess.run([
            "rsync",
            "-az",
            str(local_follower),
            f"{login_alias}:{agent_remote_dir}/"
        ], check=True)

    cmd = [
        "ssh",
        login_alias,
        "python3",
        follower_remote_path,
        outbox_dir
    ]

    return subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0
    )


# =========================
# Event Translation
# =========================

def translate_event(event):
    if event.get("type") != "codex_rpc":
        return None

    payload = event.get("payload", {})
    method = payload.get("method")
    params = payload.get("params", {})
    msg = params.get("msg", {})
    injection_id = event.get("injection_id")

    if method == "turn/started":
        return {
            "type": "turn_started",
            "job_id": event["job_id"],
            "node_id": event["node_id"],
            "injection_id": injection_id
        }

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


# =========================
# Daemon Mode
# =========================

def run_daemon(config):
    proc = start_remote_follower(config)

    debug("Daemon started")
    debug(f"Follower PID: {proc.pid}")

    import select
    import os

    stdout_buffer = b""

    while True:
        fds = [proc.stdout, proc.stderr, sys.stdin]

        ready, _, _ = select.select(
            fds,
            [],
            [],
            0.2
        )

        debug(f"select ready: stdout={proc.stdout in ready}, stderr={proc.stderr in ready}, stdin={sys.stdin in ready}")

        # ---- Always process follower output first ----
        if proc.stdout in ready:
            debug("Reading from follower stdout")
            try:
                chunk = os.read(proc.stdout.fileno(), 4096)
            except Exception as e:
                debug(f"stdout read error: {e}")
                chunk = None

            if chunk:
                debug(f"Read {len(chunk)} bytes from stdout")
                stdout_buffer += chunk

                while b"\n" in stdout_buffer:
                    line, stdout_buffer = stdout_buffer.split(b"\n", 1)
                    line = line.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue

                    debug(f"Raw event line: {line}")

                    try:
                        event = json.loads(line)
                    except Exception as e:
                        debug(f"JSON parse error: {e}")
                        continue

                    translated = translate_event(event)
                    debug(f"Translated: {translated}")
                    if translated:
                        print(json.dumps(translated), flush=True)

        if proc.stderr in ready:
            debug("Reading from follower stderr")
            try:
                err = os.read(proc.stderr.fileno(), 4096)
                if err:
                    debug(f"Follower stderr: {err.decode(errors='replace').strip()}")
            except Exception as e:
                debug(f"stderr read error: {e}")

        # ---- Then handle stdin control ----
        if sys.stdin in ready:
            line = sys.stdin.readline()
            if line:
                debug(f"Received stdin line: {line.strip()}")
                try:
                    cmd = json.loads(line.strip())
                except Exception as e:
                    debug(f"Stdin JSON parse error: {e}")
                    cmd = None

                if cmd and cmd.get("action") == "inject":
                    debug(f"Processing inject command: {cmd}")

                    # Generate injection_id immediately
                    injection_id = str(uuid.uuid4())

                    # Emit ack immediately (non-blocking control plane)
                    print(json.dumps({
                        "type": "inject_ack",
                        "job_id": cmd["job_id"],
                        "node_id": cmd.get("node_id", 0),
                        "injection_id": injection_id
                    }), flush=True)

                    # Capture values explicitly to avoid closure/race issues
                    job_id = cmd["job_id"]
                    node_id = cmd.get("node_id", 0)
                    content = cmd["content"]

                    # Perform SSH injection in background thread with delivery reporting
                    import threading

                    def do_inject(job_id, node_id, content, injection_id):
                        try:
                            login_alias = config["ssh"]["login_alias"]
                            workspace_root = config["cluster"]["workspace_root"]
                            cluster_subdir = config["cluster"]["cluster_subdir"]

                            inbox_path = f"{workspace_root}/{cluster_subdir}/mailbox/inbox/{job_id}_{int(node_id):02d}.jsonl"

                            payload = {
                                "type": "user",
                                "content": content,
                                "injection_id": injection_id
                            }

                            json_line = json.dumps(payload)
                            remote_cmd = f"printf '%s\\n' {shlex.quote(json_line)} >> {inbox_path}"

                            timeout = config.get("router", {}).get("inject_timeout_seconds", 60)

                            result = subprocess.run(
                                ["ssh", login_alias, remote_cmd],
                                capture_output=True,
                                text=True,
                                timeout=timeout
                            )

                            if result.returncode == 0:
                                print(json.dumps({
                                    "type": "inject_delivered",
                                    "job_id": job_id,
                                    "node_id": node_id,
                                    "injection_id": injection_id
                                }), flush=True)
                            else:
                                print(json.dumps({
                                    "type": "inject_failed",
                                    "job_id": job_id,
                                    "node_id": node_id,
                                    "injection_id": injection_id,
                                    "error": result.stderr.strip()
                                }), flush=True)

                        except Exception as e:
                            print(json.dumps({
                                "type": "inject_failed",
                                "job_id": job_id,
                                "node_id": node_id,
                                "injection_id": injection_id,
                                "error": str(e)
                            }), flush=True)
                            debug(f"Background inject exception: {e}")

                    threading.Thread(
                        target=do_inject,
                        args=(job_id, node_id, content, injection_id),
                        daemon=True
                    ).start()

        if proc.poll() is not None:
            print(json.dumps({"type": "follower_exit"}), flush=True)
            break


# =========================
# CLI Entry
# =========================

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--inject", action="store_true")
    parser.add_argument("--daemon", action="store_true")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--job-id")
    parser.add_argument("--node", default=0)
    parser.add_argument("--text")

    args = parser.parse_args()
    config = load_config(args.config)

    DEBUG = args.debug

    if args.daemon:
        run_daemon(config)
        sys.exit(0)

    if args.inject:
        if not args.job_id or not args.text:
            print("--inject requires --job-id and --text")
            sys.exit(1)

        injection_id = inject_prompt_internal(
            config,
            args.job_id,
            args.node,
            args.text
        )

        print(f"Injected prompt to job {args.job_id} node {int(args.node):02d}.")
        print(f"injection_id={injection_id}")
        sys.exit(0)

    print("No mode specified. Use --daemon or --inject.")
