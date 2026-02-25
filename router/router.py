import argparse
import subprocess
import json
import sys
import uuid
import shlex
import os
import select
from pathlib import Path
from datetime import datetime, timezone
import threading

sys.path.append(str(Path(__file__).resolve().parents[1]))
from common.config import load_config


# ================================
# Protocol
# ================================

PROTOCOL = "codeswarm.router.v1"
DEBUG = False


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def emit_event(event_name, data):
    envelope = {
        "protocol": PROTOCOL,
        "type": "event",
        "timestamp": now_iso(),
        "event": event_name,
        "data": data
    }
    print(json.dumps(envelope), flush=True)


def debug_event(message):
    if DEBUG:
        emit_event("debug", {
            "source": "router",
            "message": message
        })


# ================================
# Remote Follower
# ================================

def start_remote_follower(config):
    login_alias = config["ssh"]["login_alias"]
    workspace_root = config["cluster"]["workspace_root"]
    cluster_subdir = config["cluster"]["cluster_subdir"]

    outbox_dir = f"{workspace_root}/{cluster_subdir}/mailbox/outbox"

    remote_cmd = (
        f"python3 {workspace_root}/{cluster_subdir}/agent/outbox_follower.py "
        f"{outbox_dir}"
    )

    return subprocess.Popen(
        ["ssh", login_alias, remote_cmd],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0
    )


# ================================
# Translation Layer
# ================================

def translate_event(event):
    if event.get("type") != "codex_rpc":
        return None

    payload = event.get("payload", {})
    method = payload.get("method")

    job_id = event.get("job_id")
    node_id = event.get("node_id")
    injection_id = event.get("injection_id")

    # ----------------------------
    # Turn lifecycle
    # ----------------------------

    if method == "turn/started":
        return ("turn_started", {
            "job_id": job_id,
            "node_id": node_id,
            "injection_id": injection_id
        })

    if method == "turn/completed":
        return ("turn_complete", {
            "job_id": job_id,
            "node_id": node_id,
            "injection_id": injection_id
        })

    # ----------------------------
    # Assistant streaming
    # ----------------------------

    if method == "codex/event/agent_message_content_delta":
        delta = payload["params"]["msg"].get("delta")
        return ("assistant_delta", {
            "job_id": job_id,
            "node_id": node_id,
            "injection_id": injection_id,
            "content": delta
        })

    if method == "codex/event/agent_message":
        message = payload["params"]["msg"].get("message")
        return ("assistant", {
            "job_id": job_id,
            "node_id": node_id,
            "injection_id": injection_id,
            "content": message
        })

    # Fallback: task_complete contains final message
    if method == "codex/event/task_complete":
        message = payload["params"]["msg"].get("last_agent_message")
        if message:
            return ("assistant", {
                "job_id": job_id,
                "node_id": node_id,
                "injection_id": injection_id,
                "content": message
            })

    # ----------------------------
    # Token usage
    # ----------------------------

    if method == "codex/event/token_count":
        info = payload["params"]["msg"].get("info")
        if info and "total_token_usage" in info:
            total = info["total_token_usage"].get("total_tokens")
            return ("usage", {
                "job_id": job_id,
                "node_id": node_id,
                "injection_id": injection_id,
                "total_tokens": total
            })

    # Newer schema: thread/tokenUsage/updated
    if method == "thread/tokenUsage/updated":
        token_usage = payload["params"].get("tokenUsage", {})
        total = token_usage.get("total", {}).get("totalTokens")
        if total:
            return ("usage", {
                "job_id": job_id,
                "node_id": node_id,
                "injection_id": injection_id,
                "total_tokens": total
            })

    return None


# ================================
# Injection
# ================================

def perform_injection(config, request_id, job_id, node_id, content, injection_id):
    try:
        login_alias = config["ssh"]["login_alias"]
        workspace_root = config["cluster"]["workspace_root"]
        cluster_subdir = config["cluster"]["cluster_subdir"]

        inbox_path = (
            f"{workspace_root}/{cluster_subdir}/mailbox/inbox/"
            f"{job_id}_{int(node_id):02d}.jsonl"
        )

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
            emit_event("inject_delivered", {
                "request_id": request_id,
                "injection_id": injection_id,
                "job_id": job_id,
                "node_id": node_id
            })
        else:
            emit_event("inject_failed", {
                "request_id": request_id,
                "injection_id": injection_id,
                "job_id": job_id,
                "node_id": node_id,
                "error": result.stderr.strip()
            })

    except Exception as e:
        emit_event("inject_failed", {
            "request_id": request_id,
            "injection_id": injection_id,
            "job_id": job_id,
            "node_id": node_id,
            "error": str(e)
        })


# ================================
# Daemon
# ================================

def run_daemon(config):
    proc = start_remote_follower(config)

    stdout_buffer = b""
    stdin_buffer = b""

    # Make stdin non-blocking (safe because we use os.read and never readline)
    try:
        import fcntl
        fd = sys.stdin.fileno()
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
    except Exception:
        pass

    debug_event("daemon_started")

    while True:
        # Always monitor stdin; non-blocking mode prevents TTY stalls
        ready, _, _ = select.select(
            [proc.stdout, proc.stderr, sys.stdin],
            [],
            [],
            0.2
        )

        # ----------------------------
        # Follower stdout (non-blocking)
        # ----------------------------
        if proc.stdout in ready:
            chunk = os.read(proc.stdout.fileno(), 4096)
            if chunk:
                stdout_buffer += chunk

                while b"\n" in stdout_buffer:
                    line, stdout_buffer = stdout_buffer.split(b"\n", 1)
                    line = line.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue

                    try:
                        event = json.loads(line)
                    except:
                        debug_event("invalid_json_from_worker")
                        continue

                    translated = translate_event(event)
                    if translated:
                        event_name, data = translated
                        emit_event(event_name, data)

        # ----------------------------
        # Follower stderr
        # ----------------------------
        if proc.stderr in ready:
            err = os.read(proc.stderr.fileno(), 4096)
            if err:
                debug_event(f"follower_stderr: {err.decode(errors='replace')}")

        # ----------------------------
        # Stdin commands (non-blocking)
        # ----------------------------
        if sys.stdin in ready:
            try:
                chunk = os.read(sys.stdin.fileno(), 4096)
            except BlockingIOError:
                chunk = None
            except OSError:
                chunk = None

            if chunk:
                stdin_buffer += chunk

                while b"\n" in stdin_buffer:
                    line, stdin_buffer = stdin_buffer.split(b"\n", 1)
                    line = line.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue

                    # Ignore anything that is not valid JSON
                    try:
                        cmd = json.loads(line)
                    except Exception:
                        continue

                    # Ignore non-protocol lines silently
                    if cmd.get("protocol") != PROTOCOL:
                        continue

                    if cmd.get("type") != "command":
                        emit_event("command_rejected", {
                            "request_id": cmd.get("request_id"),
                            "reason": "invalid type"
                        })
                        continue

                    command = cmd.get("command")
                    request_id = cmd.get("request_id")
                    payload = cmd.get("payload", {})

                    if command == "inject":
                        job_id = payload.get("job_id")
                        node_id = payload.get("node_id", 0)
                        content = payload.get("content")

                        injection_id = str(uuid.uuid4())

                        emit_event("inject_ack", {
                            "request_id": request_id,
                            "injection_id": injection_id,
                            "job_id": job_id,
                            "node_id": node_id
                        })

                        threading.Thread(
                            target=perform_injection,
                            args=(config, request_id, job_id, node_id, content, injection_id),
                            daemon=True
                        ).start()

                    else:
                        emit_event("command_rejected", {
                            "request_id": request_id,
                            "reason": "unknown command"
                        })


# ================================
# Main
# ================================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--daemon", action="store_true")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    global DEBUG
    DEBUG = args.debug

    config = load_config(args.config)

    if args.daemon:
        run_daemon(config)


if __name__ == "__main__":
    main()
