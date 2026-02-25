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


# ================================
# Protocol
# ================================

PROTOCOL = "codeswarm.router.v1"
DEBUG = False

SWARMS = {}
JOB_TO_SWARM = {}
LAST_USAGE = {}


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

    job_id = event.get("job_id")
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

    return None


# ================================
# Daemon Loop
# ================================

def run_daemon(config):
    proc = start_remote_follower(config)

    stdout_buffer = b""
    stdin_buffer = b""

    # non-blocking stdin
    try:
        import fcntl
        fd = sys.stdin.fileno()
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
    except:
        pass

    debug_event("daemon_started")

    while True:
        ready, _, _ = select.select(
            [proc.stdout, proc.stderr, sys.stdin],
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

        # Stdin commands
        if sys.stdin in ready:
            try:
                chunk = os.read(sys.stdin.fileno(), 4096)
            except:
                chunk = None

            if chunk:
                stdin_buffer += chunk

                while b"\n" in stdin_buffer:
                    line, stdin_buffer = stdin_buffer.split(b"\n", 1)
                    line = line.decode().strip()
                    if not line:
                        continue

                    try:
                        cmd = json.loads(line)
                    except:
                        continue

                    if cmd.get("protocol") != PROTOCOL:
                        continue

                    command = cmd.get("command")
                    request_id = cmd.get("request_id")
                    payload = cmd.get("payload", {})

                    # swarm_launch
                    if command == "swarm_launch":
                        nodes = payload.get("nodes", 1)
                        partition = payload.get("partition")
                        time_limit = payload.get("time", "00:01:00")
                        account = payload.get("account")
                        qos = payload.get("qos")
                        system_prompt = payload.get("system_prompt", "")

                        if not partition:
                            emit_event("command_rejected", {
                                "request_id": request_id,
                                "reason": "partition is required"
                            })
                            continue

                        job_id = launch_swarm(config, nodes, partition, time_limit, account, qos)
                        swarm_id = str(uuid.uuid4())

                        SWARMS[swarm_id] = {
                            "job_id": job_id,
                            "node_count": nodes,
                            "system_prompt": system_prompt,
                            "status": "running"
                        }

                        JOB_TO_SWARM[job_id] = swarm_id

                        emit_event("swarm_launched", {
                            "request_id": request_id,
                            "swarm_id": swarm_id,
                            "job_id": job_id,
                            "node_count": nodes,
                            "partition": partition,
                            "time": time_limit
                        })

                        # inject system prompt
                        for node_id in range(nodes):
                            threading.Thread(
                                target=perform_injection,
                                args=(config, request_id, swarm_id, job_id, node_id, system_prompt),
                                daemon=True
                            ).start()

                    # inject
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

                    # swarm_list
                    elif command == "swarm_list":
                        emit_event("swarm_list", {
                            "request_id": request_id,
                            "swarms": SWARMS
                        })

                    # swarm_status
                    elif command == "swarm_status":
                        swarm_id = payload.get("swarm_id")
                        swarm = SWARMS.get(swarm_id)

                        if not swarm:
                            emit_event("command_rejected", {
                                "request_id": request_id,
                                "reason": "unknown swarm_id"
                            })
                            continue

                        job_id = swarm["job_id"]
                        login_alias = config["ssh"]["login_alias"]

                        result = subprocess.run(
                            ["ssh", login_alias, f"squeue -j {job_id} -h -o %T"],
                            capture_output=True,
                            text=True
                        )

                        slurm_state = result.stdout.strip() if result.stdout else "UNKNOWN"

                        emit_event("swarm_status", {
                            "request_id": request_id,
                            "swarm_id": swarm_id,
                            "job_id": job_id,
                            "node_count": swarm["node_count"],
                            "status": swarm["status"],
                            "slurm_state": slurm_state
                        })

                    # swarm_terminate
                    elif command == "swarm_terminate":
                        swarm_id = payload.get("swarm_id")
                        swarm = SWARMS.get(swarm_id)
                        if swarm:
                            job_id = swarm["job_id"]
                            login_alias = config["ssh"]["login_alias"]
                            subprocess.run(["ssh", login_alias, f"scancel {job_id}"])
                            swarm["status"] = "terminated"
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

    if args.daemon:
        run_daemon(config)


if __name__ == "__main__":
    main()

