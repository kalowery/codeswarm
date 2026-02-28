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
import re

sys.path.append(str(Path(__file__).resolve().parents[1]))
from common.config import load_config
from cluster.factory import build_provider


# ================================
# Protocol
# ================================

PROTOCOL = "codeswarm.router.v1"
DEBUG = False

SWARMS = {}
JOB_TO_SWARM = {}
LAST_USAGE = {}

STATE_FILE = Path(__file__).resolve().parents[1] / "router_state.json"


def save_state():
    try:
        data = {"swarms": SWARMS}
        with open(STATE_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


def load_state():
    global SWARMS
    try:
        if STATE_FILE.exists():
            with open(STATE_FILE) as f:
                data = json.load(f)
                SWARMS = data.get("swarms", {})
    except Exception:
        SWARMS = {}


def reconcile(provider):
    global JOB_TO_SWARM

    running_jobs = provider.list_active_jobs()

    JOB_TO_SWARM.clear()

    to_remove = []

    for swarm_id, swarm in SWARMS.items():
        job_id = swarm.get("job_id")
        if job_id in running_jobs:
            swarm["status"] = "running"
            JOB_TO_SWARM[job_id] = swarm_id
        else:
            to_remove.append((swarm_id, job_id))

    # Remove terminated swarms from memory
    for swarm_id, job_id in to_remove:
        SWARMS.pop(swarm_id, None)
        if job_id:
            JOB_TO_SWARM.pop(job_id, None)

    save_state()


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

    line = json.dumps(envelope) + "\n"

    dead = []

    for conn in TCP_CLIENTS:
        try:
            conn.sendall(line.encode())
        except:
            dead.append(conn)

    for conn in dead:
        if conn in TCP_CLIENTS:
            TCP_CLIENTS.remove(conn)

    if DEBUG:
        print(line, end="", flush=True)


def debug_event(message):
    if DEBUG:
        emit_event("debug", {"source": "router", "message": message})


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
# Slurm Allocation
# ================================

def launch_swarm(config, nodes, partition, time_limit, account=None, qos=None):
    config_path = config.get("_config_path")
    if not config_path:
        raise RuntimeError("Router config path not available for swarm launch")

    if not partition:
        raise RuntimeError("Swarm launch requires 'partition'")

    if not time_limit:
        raise RuntimeError("Swarm launch requires 'time'")

    # Local path to allocate_and_prepare.py
    repo_root = Path(__file__).resolve().parents[1]
    allocate_script = repo_root / "slurm" / "allocate_and_prepare.py"

    cmd = [
        "python3",
        str(allocate_script),
        "--config",
        config_path,
        "--nodes",
        str(nodes),
        "--time",
        str(time_limit),
        "--partition",
        str(partition),
        "--launch-codex-run"
    ]

    if account:
        cmd += ["--account", str(account)]

    if qos:
        cmd += ["--qos", str(qos)]

    result = subprocess.run(cmd, capture_output=True, text=True)

    # If allocation script failed, propagate error
    if result.returncode != 0:
        raise RuntimeError(
            f"Swarm launch failed (exit {result.returncode}).\n"
            f"STDOUT:\n{result.stdout}\n"
            f"STDERR:\n{result.stderr}"
        )

    output = result.stdout + result.stderr

    match = re.search(r"JOB_ID=(\d+)", output)
    if not match:
        match = re.search(r"Submitted job (\d+)", output)

    if not match:
        raise RuntimeError(f"Unable to parse Slurm JOB_ID. Output:\n{output}")

    return match.group(1)


# ================================
# Injection
# ================================

def perform_injection(config, request_id, swarm_id, job_id, node_id, content):
    injection_id = str(uuid.uuid4())

    emit_event("inject_ack", {
        "request_id": request_id,
        "swarm_id": swarm_id,
        "injection_id": injection_id,
        "node_id": node_id
    })

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
                "swarm_id": swarm_id,
                "injection_id": injection_id,
                "node_id": node_id
            })
        else:
            emit_event("inject_failed", {
                "request_id": request_id,
                "swarm_id": swarm_id,
                "injection_id": injection_id,
                "node_id": node_id,
                "error": result.stderr.strip()
            })

    except Exception as e:
        emit_event("inject_failed", {
            "request_id": request_id,
            "swarm_id": swarm_id,
            "injection_id": injection_id,
            "node_id": node_id,
            "error": str(e)
        })


# ================================
# Translation
# ================================

def translate_event(event):
    if event.get("type") != "codex_rpc":
        return None

    job_id = str(event.get("job_id"))
    node_id = event.get("node_id")
    injection_id = event.get("injection_id")
    swarm_id = JOB_TO_SWARM.get(job_id)

    # Ignore events from jobs not tracked by this router instance
    if not swarm_id:
        return None

    payload = event.get("payload", {})
    method = payload.get("method")

    base = {
        "swarm_id": swarm_id,
        "job_id": job_id,
        "node_id": node_id,
        "injection_id": injection_id
    }

    if method == "turn/started":
        return ("turn_started", base)

    if method == "turn/completed":
        return ("turn_complete", base)

    if method == "codex/event/agent_message_content_delta":
        delta = payload["params"]["msg"].get("delta")
        return ("assistant_delta", {**base, "content": delta})

    if method == "codex/event/agent_message":
        msg = payload["params"]["msg"].get("message")
        return ("assistant", {**base, "content": msg})

    if method == "codex/event/token_count":
        info = payload["params"]["msg"].get("info")
        if info:
            total = info["total_token_usage"]["total_tokens"]
            last = LAST_USAGE.get(injection_id)
            if last == total:
                return None
            LAST_USAGE[injection_id] = total
            return ("usage", {**base, "total_tokens": total})

    if method == "thread/tokenUsage/updated":
        total = payload["params"]["tokenUsage"]["total"]["totalTokens"]
        last = LAST_USAGE.get(injection_id)
        if last == total:
            return None
        LAST_USAGE[injection_id] = total
        return ("usage", {**base, "total_tokens": total})

    # --- Task lifecycle normalization ---
    if method == "codex/event/task_started":
        return (
            "task_started",
            {
                **base,
                "raw": payload
            }
        )

    if method == "codex/event/task_complete":
        msg = payload.get("params", {}).get("msg", {})
        return (
            "task_complete",
            {
                **base,
                "last_agent_message": msg.get("last_agent_message"),
                "raw": payload
            }
        )

    # --- Error normalization ---
    if method == "codex/event/error":
        msg = payload.get("params", {}).get("msg", {})
        return (
            "agent_error",
            {
                **base,
                "message": msg.get("message"),
                "error_code": msg.get("codex_error_info"),
                "raw": payload
            }
        )

    if method == "error":
        err = payload.get("params", {}).get("error", {})
        return (
            "agent_error",
            {
                **base,
                "message": err.get("message"),
                "error_code": err.get("codexErrorInfo"),
                "raw": payload
            }
        )

    # --- Command execution normalization ---
    if method == "codex/event/exec_command_begin":
        msg = payload.get("params", {}).get("msg", {})
        return (
            "command_started",
            {
                **base,
                "call_id": msg.get("call_id"),
                "command": msg.get("command"),
                "cwd": msg.get("cwd"),
                "raw": payload
            }
        )

    if method == "codex/event/exec_command_end":
        msg = payload.get("params", {}).get("msg", {})
        return (
            "command_completed",
            {
                **base,
                "call_id": msg.get("call_id"),
                "command": msg.get("command"),
                "cwd": msg.get("cwd"),
                "stdout": msg.get("stdout"),
                "stderr": msg.get("stderr"),
                "exit_code": msg.get("exit_code"),
                "duration": msg.get("duration"),
                "raw": payload
            }
        )

    # --- Reasoning normalization ---
    if method == "codex/event/agent_reasoning_delta":
        msg = payload.get("params", {}).get("msg", {})
        return (
            "reasoning_delta",
            {
                **base,
                "content": msg.get("delta"),
                "raw": payload
            }
        )

    if method == "codex/event/agent_reasoning":
        msg = payload.get("params", {}).get("msg", {})
        return (
            "reasoning",
            {
                **base,
                "content": msg.get("text"),
                "raw": payload
            }
        )

    # ---- Unknown method debugging ----
    if DEBUG and method:
        print(
            f"[router DEBUG] UNHANDLED METHOD: {method} | payload={json.dumps(payload)}",
            flush=True
        )

    return None


# ================================
# Daemon Loop
# ================================

import queue
COMMAND_QUEUE = queue.Queue()
TCP_CLIENTS = []

def run_daemon(config, provider):

    stdout_buffer = b""

    # TCP control server
    import socket, sys

    def tcp_server():
        host = "127.0.0.1"
        port = 8765

        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((host, port))
        server.listen()

        print(f"TCP CONTROL READY {host}:{port}", file=sys.stderr, flush=True)

        while True:
            conn, addr = server.accept()
            threading.Thread(target=handle_client, args=(conn,), daemon=True).start()

    def handle_client(conn):
        print("CLIENT CONNECTED", file=sys.stderr, flush=True)
        TCP_CLIENTS.append(conn)
        buffer = b""
        try:
            while True:
                try:
                    chunk = conn.recv(4096)
                except ConnectionResetError:
                    break

                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    decoded = line.decode().strip()
                    if decoded:
                        COMMAND_QUEUE.put(decoded)
        finally:
            if conn in TCP_CLIENTS:
                TCP_CLIENTS.remove(conn)
            conn.close()

    threading.Thread(target=tcp_server, daemon=True).start()

    # Start follower asynchronously so it cannot block daemon startup
    proc = None

    def start_follower_async():
        nonlocal proc
        try:
            proc = start_remote_follower(config)
        except Exception as e:
            print(f"Follower failed to start: {e}", flush=True)

    threading.Thread(target=start_follower_async, daemon=True).start()

    debug_event("daemon_started")

    while True:
        streams = []
        if proc:
            streams = [proc.stdout, proc.stderr]

        ready, _, _ = select.select(
            streams,
            [],
            [],
            0.2
        )

        # Follower stdout
        if proc.stdout in ready:
            chunk = os.read(proc.stdout.fileno(), 4096)
            if chunk:
                stdout_buffer += chunk

                while b"\n" in stdout_buffer:
                    line, stdout_buffer = stdout_buffer.split(b"\n", 1)
                    line = line.decode().strip()
                    if not line:
                        continue

                    try:
                        event = json.loads(line)
                    except:
                        continue

                    translated = translate_event(event)
                    if translated:
                        event_name, data = translated
                        emit_event(event_name, data)

        # Process queued stdin commands
# Process queued stdin commands
        while not COMMAND_QUEUE.empty():
            raw = COMMAND_QUEUE.get()

            try:
                cmd = json.loads(raw)
            except:
                continue

            if cmd.get("protocol") != PROTOCOL:
                continue

            command = cmd.get("command")
            request_id = cmd.get("request_id")
            payload = cmd.get("payload", {})

            debug_event(f"command received: {command!r}")

            if command == "swarm_launch":
                nodes = payload.get("nodes", 1)
                system_prompt = payload.get("system_prompt", "")

                try:
                    job_id = provider.launch(nodes)
                except Exception as e:
                    emit_event("command_rejected", {
                        "request_id": request_id,
                        "reason": str(e)
                    })
                    continue

                swarm_id = str(uuid.uuid4())

                SWARMS[swarm_id] = {
                    "job_id": job_id,
                    "node_count": nodes,
                    "system_prompt": system_prompt,
                    "status": "running",
                    "backend": config.get("cluster", {}).get("backend", "slurm")
                }

                JOB_TO_SWARM[job_id] = swarm_id
                save_state()

                emit_event("swarm_launched", {
                    "request_id": request_id,
                    "swarm_id": swarm_id,
                    "job_id": job_id,
                    "node_count": nodes
                })

                for node_id in range(nodes):
                    threading.Thread(
                        target=perform_injection,
                        args=(config, request_id, swarm_id, job_id, node_id, system_prompt),
                        daemon=True
                    ).start()

            elif command == "inject":
                swarm_id = payload.get("swarm_id")
                nodes = payload.get("nodes", "all")
                content = payload.get("content")

                swarm = SWARMS.get(swarm_id)
                if not swarm:
                    continue

                job_id = swarm["job_id"]
                node_count = swarm["node_count"]

                if nodes == "all":
                    targets = range(node_count)
                elif isinstance(nodes, list):
                    targets = nodes
                else:
                    targets = [nodes]

                for node_id in targets:
                    threading.Thread(
                        target=perform_injection,
                        args=(config, request_id, swarm_id, job_id, node_id, content),
                        daemon=True
                    ).start()

            elif command == "swarm_list":
                emit_event("swarm_list", {
                    "request_id": request_id,
                    "swarms": SWARMS
                })

            elif command == "swarm_status":
                swarm_id = payload.get("swarm_id")
                swarm = SWARMS.get(swarm_id)

                if not swarm:
                    emit_event("command_rejected", {
                        "request_id": request_id,
                        "reason": "unknown swarm_id"
                    })
                    continue

                def handle_swarm_status():
                    try:
                        job_id = swarm.get("job_id")

                        # If job_id is missing, swarm is considered terminated
                        if not job_id:
                            swarm["status"] = "terminated"
                            save_state()
                            emit_event("swarm_status", {
                                "request_id": request_id,
                                "swarm_id": swarm_id,
                                "job_id": None,
                                "node_count": swarm.get("node_count"),
                                "status": "terminated",
                                "slurm_state": "MISSING_JOB_ID"
                            })
                            return

                        login_alias = config["ssh"]["login_alias"]

                        result = subprocess.run(
                            ["ssh", login_alias, f"squeue -j {job_id} -h -o '%T'"],
                            capture_output=True,
                            text=True,
                            timeout=15
                        )

                        slurm_state = result.stdout.strip()

                        # If job not found in Slurm, remove swarm entirely
                        if not slurm_state:
                            slurm_state = "NOT_FOUND"

                            # Remove terminated swarm from memory
                            SWARMS.pop(swarm_id, None)
                            JOB_TO_SWARM.pop(job_id, None)
                            save_state()

                            emit_event("swarm_status", {
                                "request_id": request_id,
                                "swarm_id": swarm_id,
                                "job_id": job_id,
                                "node_count": swarm.get("node_count"),
                                "status": "terminated",
                                "slurm_state": slurm_state
                            })
                            return
                        else:
                            swarm["status"] = "running"
                            save_state()

                            emit_event("swarm_status", {
                                "request_id": request_id,
                                "swarm_id": swarm_id,
                                "job_id": job_id,
                                "node_count": swarm["node_count"],
                                "status": swarm["status"],
                                "slurm_state": slurm_state
                            })
                    except Exception as e:
                        emit_event("swarm_status", {
                            "request_id": request_id,
                            "swarm_id": swarm_id,
                            "error": str(e)
                        })

                threading.Thread(target=handle_swarm_status, daemon=True).start()

            elif command == "swarm_terminate":
                swarm_id = payload.get("swarm_id")
                swarm = SWARMS.get(swarm_id)
                if swarm:
                    job_id = swarm["job_id"]
                    login_alias = config["ssh"]["login_alias"]
                    subprocess.run(["ssh", login_alias, f"scancel {job_id}"])

                    # Remove swarm immediately after termination
                    SWARMS.pop(swarm_id, None)
                    JOB_TO_SWARM.pop(job_id, None)
                    save_state()

                    emit_event("swarm_terminated", {
                        "request_id": request_id,
                        "swarm_id": swarm_id
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

    # Store absolute config path so swarm_launch uses the same config
    config["_config_path"] = str(Path(args.config).resolve())

    # Load persisted state and reconcile with cluster backend
    load_state()
    provider = build_provider(config)
    reconcile(provider)

    # Ensure state is flushed on shutdown
    import signal

    def graceful_shutdown(signum, frame):
        save_state()
        sys.exit(0)

    signal.signal(signal.SIGTERM, graceful_shutdown)
    signal.signal(signal.SIGINT, graceful_shutdown)

    if args.daemon:
        run_daemon(config, provider)


if __name__ == "__main__":
    main()

