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
import time
from collections import defaultdict, deque

sys.path.append(str(Path(__file__).resolve().parents[1]))
from common.config import load_config
from .cluster.factory import build_provider


# ================================
# Protocol
# ================================

PROTOCOL = "codeswarm.router.v1"
DEBUG = False

SWARMS = {}
JOB_TO_SWARM = {}
LAST_USAGE = {}
PENDING_APPROVALS = {}
PENDING_APPROVAL_DECISIONS = {}
ACTIVE_PROVIDER = None
NODE_OUTSTANDING = defaultdict(int)
NODE_THREAD_ACTIVE = defaultdict(bool)
INTER_SWARM_QUEUE = defaultdict(deque)
SCHEDULER_LOCK = threading.Lock()

# Retention policy
TERMINATED_TTL_SECONDS = 900  # 15 minutes
MAX_TERMINATED = 100

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

    # Mark terminated instead of immediate removal
    for swarm_id, job_id in to_remove:
        swarm = SWARMS.get(swarm_id)
        if swarm and swarm.get("status") != "terminated":
            swarm["status"] = "terminated"
            swarm["terminated_at"] = time.time()
        if job_id:
            JOB_TO_SWARM.pop(job_id, None)

    save_state()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def cleanup_terminated():
    now = time.time()

    terminated = [
        (sid, s)
        for sid, s in SWARMS.items()
        if s.get("status") == "terminated"
    ]

    # TTL prune
    for sid, s in list(terminated):
        if now - s.get("terminated_at", now) > TERMINATED_TTL_SECONDS:
            SWARMS.pop(sid, None)
            emit_event("swarm_removed", {"swarm_id": sid})

    # Hard cap
    terminated = [
        (sid, s)
        for sid, s in SWARMS.items()
        if s.get("status") == "terminated"
    ]

    if len(terminated) > MAX_TERMINATED:
        terminated.sort(key=lambda x: x[1].get("terminated_at", 0))
        overflow = terminated[:-MAX_TERMINATED]

        for sid, _ in overflow:
            SWARMS.pop(sid, None)
            emit_event("swarm_removed", {"swarm_id": sid})

    save_state()


def cleanup_loop():
    while True:
        time.sleep(60)
        try:
            cleanup_terminated()
        except Exception:
            pass

threading.Thread(target=cleanup_loop, daemon=True).start()


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


def _queue_snapshot():
    with SCHEDULER_LOCK:
        items = []
        for target_swarm_id, q in INTER_SWARM_QUEUE.items():
            for item in q:
                items.append({
                    "queue_id": item.get("queue_id"),
                    "request_id": item.get("request_id"),
                    "source_swarm_id": item.get("source_swarm_id"),
                    "target_swarm_id": target_swarm_id,
                    "selector": item.get("selector"),
                    "nodes": item.get("nodes"),
                    "content": item.get("content"),
                    "created_at": item.get("created_at"),
                })
        return items


def _emit_queue_updated():
    emit_event("queue_updated", {
        "items": _queue_snapshot()
    })


def _node_key(swarm_id, node_id):
    return (str(swarm_id), int(node_id))


def _mark_outstanding(swarm_id, node_id, delta):
    key = _node_key(swarm_id, node_id)
    with SCHEDULER_LOCK:
        NODE_OUTSTANDING[key] = max(0, int(NODE_OUTSTANDING.get(key, 0)) + int(delta))


def _first_idle_node_id(swarm_id):
    swarm = SWARMS.get(str(swarm_id))
    if not swarm:
        return None
    node_count = int(swarm.get("node_count") or 0)
    fallback_node = None
    for node_id in range(node_count):
        key = _node_key(swarm_id, node_id)
        if int(NODE_OUTSTANDING.get(key, 0)) == 0:
            if not bool(NODE_THREAD_ACTIVE.get(key, False)):
                return node_id
            if fallback_node is None:
                fallback_node = node_id
    if fallback_node is not None:
        return fallback_node
    return None


def _dispatch_inter_swarm_queue(config, provider):
    """
    Route queued inter-swarm work to the first idle node in each target swarm.
    """
    with SCHEDULER_LOCK:
        target_ids = list(INTER_SWARM_QUEUE.keys())

    for target_swarm_id in target_ids:
        while True:
            with SCHEDULER_LOCK:
                queue_for_target = INTER_SWARM_QUEUE.get(target_swarm_id)
                if not queue_for_target:
                    break
                item = queue_for_target[0]

            target_swarm = SWARMS.get(str(target_swarm_id))
            if not target_swarm or target_swarm.get("status") == "terminated":
                with SCHEDULER_LOCK:
                    INTER_SWARM_QUEUE[target_swarm_id].popleft()
                    if not INTER_SWARM_QUEUE[target_swarm_id]:
                        INTER_SWARM_QUEUE.pop(target_swarm_id, None)
                emit_event("inter_swarm_dropped", {
                    "queue_id": item.get("queue_id"),
                    "source_swarm_id": item.get("source_swarm_id"),
                    "target_swarm_id": target_swarm_id,
                    "reason": "target swarm unavailable",
                })
                _emit_queue_updated()
                continue

            selector = item.get("selector") or "idle"
            if selector in ("all", "nodes"):
                job_id = target_swarm.get("job_id")
                if not job_id:
                    break
                node_count = int(target_swarm.get("node_count") or 0)
                if selector == "all":
                    targets = list(range(node_count))
                else:
                    raw_targets = item.get("nodes")
                    targets = [
                        node_id for node_id in (raw_targets if isinstance(raw_targets, list) else [])
                        if isinstance(node_id, int) and 0 <= node_id < node_count
                    ]

                if not targets:
                    with SCHEDULER_LOCK:
                        INTER_SWARM_QUEUE[target_swarm_id].popleft()
                        if not INTER_SWARM_QUEUE[target_swarm_id]:
                            INTER_SWARM_QUEUE.pop(target_swarm_id, None)
                    emit_event("inter_swarm_dropped", {
                        "queue_id": item.get("queue_id"),
                        "source_swarm_id": item.get("source_swarm_id"),
                        "target_swarm_id": target_swarm_id,
                        "reason": "no valid target nodes",
                    })
                    _emit_queue_updated()
                    continue

                content = item.get("content")
                request_id = item.get("request_id")
                for node_id in targets:
                    threading.Thread(
                        target=perform_injection,
                        args=(config, provider, request_id, str(target_swarm_id), str(job_id), int(node_id), content),
                        daemon=True
                    ).start()

                with SCHEDULER_LOCK:
                    INTER_SWARM_QUEUE[target_swarm_id].popleft()
                    if not INTER_SWARM_QUEUE[target_swarm_id]:
                        INTER_SWARM_QUEUE.pop(target_swarm_id, None)

                emit_event("inter_swarm_dispatched", {
                    "queue_id": item.get("queue_id"),
                    "request_id": request_id,
                    "source_swarm_id": item.get("source_swarm_id"),
                    "target_swarm_id": target_swarm_id,
                    "selector": selector,
                    "nodes": targets if selector == "nodes" else None,
                })
                _emit_queue_updated()
                continue

            idle_node_id = _first_idle_node_id(target_swarm_id)
            if idle_node_id is None:
                break

            request_id = item.get("request_id")
            content = item.get("content")
            job_id = target_swarm.get("job_id")

            if not job_id:
                break

            # Reserve by incrementing before injection write; if inject fails it is reverted.
            _mark_outstanding(target_swarm_id, idle_node_id, +1)
            success, injection_id, error = perform_injection(
                config,
                provider,
                request_id,
                str(target_swarm_id),
                str(job_id),
                int(idle_node_id),
                content,
                count_outstanding=False,
            )
            if not success:
                _mark_outstanding(target_swarm_id, idle_node_id, -1)
                emit_event("inter_swarm_blocked", {
                    "queue_id": item.get("queue_id"),
                    "source_swarm_id": item.get("source_swarm_id"),
                    "target_swarm_id": target_swarm_id,
                    "node_id": idle_node_id,
                    "reason": error or "inject failed",
                })
                break

            with SCHEDULER_LOCK:
                INTER_SWARM_QUEUE[target_swarm_id].popleft()
                if not INTER_SWARM_QUEUE[target_swarm_id]:
                    INTER_SWARM_QUEUE.pop(target_swarm_id, None)

            emit_event("inter_swarm_dispatched", {
                "queue_id": item.get("queue_id"),
                "request_id": request_id,
                "source_swarm_id": item.get("source_swarm_id"),
                "target_swarm_id": target_swarm_id,
                "node_id": idle_node_id,
                "injection_id": injection_id,
            })
            _emit_queue_updated()


def execute_synthetic_approved_command(meta, job_id, call_id):
    """
    Execute approved synthetic command requests that originate from
    function_call bridging (no native rpc_id to resume inside app-server).
    """
    command = meta.get("command")
    cwd = meta.get("cwd")

    if not isinstance(command, str) or not command.strip():
        emit_event("command_completed", {
            "swarm_id": meta.get("swarm_id"),
            "job_id": str(job_id),
            "node_id": meta.get("node_id"),
            "injection_id": meta.get("injection_id"),
            "call_id": call_id,
            "command": command,
            "cwd": cwd,
            "stdout": "",
            "stderr": "Synthetic approval command missing executable text",
            "exit_code": 1,
            "duration": {"secs": 0, "nanos": 0},
        })
        return

    start = time.time()
    emit_event("command_started", {
        "swarm_id": meta.get("swarm_id"),
        "job_id": str(job_id),
        "node_id": meta.get("node_id"),
        "injection_id": meta.get("injection_id"),
        "call_id": call_id,
        "command": ["/bin/bash", "-lc", command],
        "cwd": cwd,
    })

    try:
        completed = subprocess.run(
            ["/bin/bash", "-lc", command],
            cwd=cwd if isinstance(cwd, str) and cwd else None,
            capture_output=True,
            text=True,
        )
        exit_code = int(completed.returncode)
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
    except Exception as e:
        exit_code = 1
        stdout = ""
        stderr = str(e)

    elapsed = time.time() - start
    secs = int(elapsed)
    nanos = int((elapsed - secs) * 1_000_000_000)

    emit_event("command_completed", {
        "swarm_id": meta.get("swarm_id"),
        "job_id": str(job_id),
        "node_id": meta.get("node_id"),
        "injection_id": meta.get("injection_id"),
        "call_id": call_id,
        "command": ["/bin/bash", "-lc", command],
        "cwd": cwd,
        "stdout": stdout,
        "stderr": stderr,
        "exit_code": exit_code,
        "duration": {"secs": secs, "nanos": nanos},
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

def perform_injection(config, provider, request_id, swarm_id, job_id, node_id, content, count_outstanding=True):
    injection_id = str(uuid.uuid4())

    emit_event("inject_ack", {
        "request_id": request_id,
        "swarm_id": swarm_id,
        "injection_id": injection_id,
        "node_id": node_id
    })

    try:
        provider.inject(job_id, node_id, content, injection_id)
        if count_outstanding:
            _mark_outstanding(swarm_id, node_id, +1)

        emit_event("inject_delivered", {
            "request_id": request_id,
            "swarm_id": swarm_id,
            "injection_id": injection_id,
            "node_id": node_id
        })
        return (True, injection_id, None)

    except Exception as e:
        emit_event("inject_failed", {
            "request_id": request_id,
            "swarm_id": swarm_id,
            "injection_id": injection_id,
            "node_id": node_id,
            "error": str(e)
        })
        return (False, injection_id, str(e))


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

    if method in ("codex/event/item_completed", "item/completed"):
        params = payload.get("params", {})
        item = params.get("item")
        if item is None:
            item = params.get("msg", {}).get("item")

        if isinstance(item, dict):
            item_type_raw = item.get("type")
            item_type = re.sub(r"[^a-z]", "", str(item_type_raw).lower())
            if item_type == "agentmessage":
                # Some runtimes deliver final assistant text on item_completed
                # without a separate codex/event/agent_message snapshot.
                content = None
                if isinstance(item.get("text"), str):
                    content = item.get("text")
                elif isinstance(item.get("message"), str):
                    content = item.get("message")
                else:
                    parts = item.get("content")
                    if isinstance(parts, list):
                        chunks = []
                        for part in parts:
                            if isinstance(part, dict):
                                text = part.get("text")
                                if isinstance(text, str):
                                    chunks.append(text)
                        if chunks:
                            content = "".join(chunks)

                if isinstance(content, str) and content:
                    return ("assistant", {**base, "content": content})
                return ("turn_complete", base)

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

    if method == "thread/status/changed":
        status = payload.get("params", {}).get("status", {})
        return ("thread_status", {**base, "status": status})

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
    if method in ("codex/event/exec_approval_request", "item/commandExecution/requestApproval"):
        params = payload.get("params", {})
        msg = params.get("msg")

        if msg:
            # Shape A: codex/event/exec_approval_request
            call_id = msg.get("call_id")
            command = msg.get("command")
            reason = msg.get("reason")
            cwd = msg.get("cwd")
            proposed_execpolicy_amendment = msg.get("proposed_execpolicy_amendment")
            available_decisions = msg.get("available_decisions")
        else:
            # Shape B: item/commandExecution/requestApproval
            call_id = params.get("itemId")
            command = params.get("command")
            reason = params.get("reason")
            cwd = params.get("cwd")
            proposed_execpolicy_amendment = params.get("proposedExecpolicyAmendment") or params.get("proposed_execpolicy_amendment")
            available_decisions = params.get("availableDecisions") or params.get("available_decisions")

        rpc_id = payload.get("id")

        if call_id:
            key = (job_id, call_id)
            existing = PENDING_APPROVALS.get(key, {})

            # Keep the strongest request shape when both legacy and request-style
            # approval events are emitted for the same call_id.
            existing_rpc_id = existing.get("rpc_id")
            merged_rpc_id = rpc_id if rpc_id is not None else existing_rpc_id

            existing_available = existing.get("available_decisions") or []
            new_available = available_decisions or []

            def _has_accept_decisions(decisions):
                return any(
                    (isinstance(d, str) and d in ("accept", "cancel")) or
                    (isinstance(d, dict) and "acceptWithExecpolicyAmendment" in d)
                    for d in (decisions or [])
                )

            merged_available = (
                new_available
                if _has_accept_decisions(new_available) or not existing_available
                else existing_available
            )

            PENDING_APPROVALS[key] = {
                "swarm_id": swarm_id,
                "node_id": node_id,
                "injection_id": injection_id,
                "rpc_id": merged_rpc_id,
                "approval_method": method,
                "command": command,
                "cwd": cwd,
                "proposed_execpolicy_amendment": proposed_execpolicy_amendment,
                "available_decisions": merged_available,
            }
            pass  # debug removed

            # If user already approved via legacy notification path, immediately
            # satisfy the later request-style approval without requiring a 2nd click.
            pending_decision = PENDING_APPROVAL_DECISIONS.get(key)
            if (
                pending_decision
                and ACTIVE_PROVIDER is not None
                and merged_rpc_id is not None
            ):
                approved_flag = bool(pending_decision.get("approved"))
                raw_decision = pending_decision.get("decision")

                if isinstance(raw_decision, str):
                    if raw_decision in ("accept", "cancel"):
                        rpc_decision = raw_decision
                    elif raw_decision == "approved":
                        rpc_decision = "accept"
                    elif raw_decision == "abort":
                        rpc_decision = "cancel"
                    else:
                        rpc_decision = "accept" if approved_flag else "cancel"
                elif (
                    isinstance(raw_decision, dict)
                    and isinstance(raw_decision.get("acceptWithExecpolicyAmendment"), dict)
                ):
                    rpc_decision = raw_decision
                elif (
                    isinstance(raw_decision, dict)
                    and isinstance(raw_decision.get("approved_execpolicy_amendment"), dict)
                ):
                    amendment = raw_decision["approved_execpolicy_amendment"].get(
                        "proposed_execpolicy_amendment"
                    )
                    rpc_decision = {
                        "acceptWithExecpolicyAmendment": {
                            "execpolicy_amendment": amendment if isinstance(amendment, list) else []
                        }
                    }
                else:
                    rpc_decision = "accept" if approved_flag else "cancel"

                ACTIVE_PROVIDER.send_control(
                    job_id,
                    node_id,
                    {
                        "type": "rpc_response",
                        "rpc_id": merged_rpc_id,
                        "result": {
                            "decision": rpc_decision,
                            "approved": approved_flag,
                        },
                    },
                )
                PENDING_APPROVAL_DECISIONS.pop(key, None)
                PENDING_APPROVALS.pop(key, None)

        return (
            "exec_approval_required",
            {
                **base,
                "call_id": call_id,
                "command": command,
                "reason": reason,
                "cwd": cwd,
                "proposed_execpolicy_amendment": proposed_execpolicy_amendment,
                "available_decisions": available_decisions,
                "raw": payload,
            },
        )

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
    global ACTIVE_PROVIDER
    ACTIVE_PROVIDER = provider

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
            proc = provider.start_follower()
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
        if proc and proc.stdout in ready:
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
                        if event_name == "thread_status":
                            status = data.get("status") or {}
                            status_type = status.get("type") if isinstance(status, dict) else None
                            key = _node_key(data.get("swarm_id"), data.get("node_id"))
                            with SCHEDULER_LOCK:
                                if status_type == "active":
                                    NODE_THREAD_ACTIVE[key] = True
                                elif status_type == "idle":
                                    NODE_THREAD_ACTIVE[key] = False
                                    # Reconcile missed turn_complete events so idle queue
                                    # dispatch cannot deadlock on stale outstanding counts.
                                    NODE_OUTSTANDING[key] = 0
                            if status_type == "idle":
                                _dispatch_inter_swarm_queue(config, provider)
                        if event_name == "turn_complete":
                            _mark_outstanding(data.get("swarm_id"), data.get("node_id"), -1)
                            _dispatch_inter_swarm_queue(config, provider)
                        if event_name == "task_complete":
                            # Some traces emit task_complete without a matching turn_complete;
                            # reconcile outstanding count to avoid idle-queue starvation.
                            _mark_outstanding(data.get("swarm_id"), data.get("node_id"), -1)
                            _dispatch_inter_swarm_queue(config, provider)
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

            pass  # debug removed

            if command == "swarm_launch":
                nodes = payload.get("nodes", 1)
                system_prompt = payload.get("system_prompt", "")
                agents_md_content = payload.get("agents_md_content")
                if not isinstance(agents_md_content, str) or not agents_md_content.strip():
                    agents_md_content = None

                try:
                    job_id = provider.launch(nodes, agents_md_content=agents_md_content)
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
                with SCHEDULER_LOCK:
                    for node_id in range(nodes):
                        NODE_THREAD_ACTIVE[_node_key(swarm_id, node_id)] = False
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
                        args=(config, provider, request_id, swarm_id, job_id, node_id, system_prompt),
                        daemon=True
                    ).start()
                _dispatch_inter_swarm_queue(config, provider)

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
                        args=(config, provider, request_id, swarm_id, job_id, node_id, content),
                        daemon=True
                    ).start()

            elif command == "enqueue_inject":
                source_swarm_id = payload.get("source_swarm_id")
                target_swarm_id = payload.get("target_swarm_id")
                selector = payload.get("selector", "idle")
                content = payload.get("content")
                nodes = payload.get("nodes")

                if not target_swarm_id or not isinstance(content, str) or not content.strip():
                    emit_event("command_rejected", {
                        "request_id": request_id,
                        "reason": "invalid enqueue payload"
                    })
                    continue

                target_swarm = SWARMS.get(str(target_swarm_id))
                if not target_swarm:
                    emit_event("command_rejected", {
                        "request_id": request_id,
                        "reason": "unknown target_swarm_id"
                    })
                    continue
                if selector not in ("idle", "all", "nodes"):
                    selector = "idle"

                queue_id = str(uuid.uuid4())
                queued_nodes = nodes if selector == "nodes" and isinstance(nodes, list) else None
                queue_item = {
                    "queue_id": queue_id,
                    "request_id": request_id,
                    "source_swarm_id": source_swarm_id,
                    "target_swarm_id": str(target_swarm_id),
                    "selector": selector,
                    "nodes": queued_nodes,
                    "content": content,
                    "created_at": time.time(),
                }
                with SCHEDULER_LOCK:
                    INTER_SWARM_QUEUE[str(target_swarm_id)].append(queue_item)

                emit_event("inter_swarm_enqueued", {
                    "request_id": request_id,
                    "queue_id": queue_id,
                    "source_swarm_id": source_swarm_id,
                    "target_swarm_id": target_swarm_id,
                    "selector": selector,
                    "nodes": queued_nodes,
                })
                _emit_queue_updated()
                _dispatch_inter_swarm_queue(config, provider)

            elif command == "queue_list":
                emit_event("queue_list", {
                    "request_id": request_id,
                    "items": _queue_snapshot()
                })
                _emit_queue_updated()

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

                        if not job_id:
                            swarm["status"] = "terminated"
                            save_state()
                            emit_event("swarm_status", {
                                "request_id": request_id,
                                "swarm_id": swarm_id,
                                "job_id": None,
                                "node_count": swarm.get("node_count"),
                                "status": "terminated"
                            })
                            return

                        state = provider.get_job_state(job_id)

                        if not state:
                            if swarm.get("status") != "terminated":
                                swarm["status"] = "terminated"
                                swarm["terminated_at"] = time.time()
                                save_state()

                            emit_event("swarm_status", {
                                "request_id": request_id,
                                "swarm_id": swarm_id,
                                "job_id": job_id,
                                "node_count": swarm.get("node_count"),
                                "status": "terminated"
                            })
                        else:
                            swarm["status"] = "running"
                            save_state()

                            emit_event("swarm_status", {
                                "request_id": request_id,
                                "swarm_id": swarm_id,
                                "job_id": job_id,
                                "node_count": swarm["node_count"],
                                "status": swarm["status"]
                            })
                    except Exception as e:
                        emit_event("swarm_status", {
                            "request_id": request_id,
                            "swarm_id": swarm_id,
                            "error": str(e)
                        })

                threading.Thread(target=handle_swarm_status, daemon=True).start()

            elif command == "approve_execution":
                job_id = payload.get("job_id")
                call_id = payload.get("call_id")
                approved = payload.get("approved")
                decision = payload.get("decision")

                key = (str(job_id), call_id)
                pass  # debug removed
                meta = PENDING_APPROVALS.get(key)

                if not meta:
                    emit_event("command_rejected", {
                        "request_id": request_id,
                        "reason": "unknown approval request"
                    })
                    continue

                try:
                    rpc_id = meta.get("rpc_id")
                    available_decisions = meta.get("available_decisions") or []

                    def _extract_amendment_from_decision(d):
                        if not isinstance(d, dict):
                            return None
                        if isinstance(d.get("approved_execpolicy_amendment"), dict):
                            inner = d["approved_execpolicy_amendment"]
                            if isinstance(inner.get("proposed_execpolicy_amendment"), list):
                                return inner["proposed_execpolicy_amendment"]
                        if isinstance(d.get("acceptWithExecpolicyAmendment"), dict):
                            inner = d["acceptWithExecpolicyAmendment"]
                            if isinstance(inner.get("execpolicy_amendment"), list):
                                return inner["execpolicy_amendment"]
                        return None

                    def _normalize_approved_decision(d, approved_flag):
                        if d is None:
                            return "approved" if approved_flag else "abort"
                        if isinstance(d, str):
                            if d in ("approved", "abort"):
                                return d
                            if d == "accept":
                                return "approved"
                            if d == "cancel":
                                return "abort"
                            return "approved" if approved_flag else "abort"
                        if isinstance(d, dict):
                            amendment = _extract_amendment_from_decision(d)
                            if amendment:
                                return {
                                    "approved_execpolicy_amendment": {
                                        "proposed_execpolicy_amendment": amendment
                                    }
                                }
                        return "approved" if approved_flag else "abort"

                    def _normalize_accept_decision(d, approved_flag):
                        if d is None:
                            return "accept" if approved_flag else "cancel"
                        if isinstance(d, str):
                            if d in ("accept", "cancel"):
                                return d
                            if d == "approved":
                                return "accept"
                            if d == "abort":
                                return "cancel"
                            return "accept" if approved_flag else "cancel"
                        if isinstance(d, dict):
                            amendment = _extract_amendment_from_decision(d)
                            if amendment:
                                return {
                                    "acceptWithExecpolicyAmendment": {
                                        "execpolicy_amendment": amendment
                                    }
                                }
                        return "accept" if approved_flag else "cancel"

                    def _normalize_decision_for_available(d, approved_flag, available):
                        """
                        Normalize UI decision into one of the runtime-supported dialects:
                        - accept/cancel (+ acceptWithExecpolicyAmendment)
                        - approved/abort (+ approved_execpolicy_amendment)
                        """
                        has_accept_local = any(
                            (isinstance(x, str) and x in ("accept", "cancel")) or
                            (isinstance(x, dict) and "acceptWithExecpolicyAmendment" in x)
                            for x in (available or [])
                        )
                        if has_accept_local:
                            return _normalize_accept_decision(d, approved_flag)
                        return _normalize_approved_decision(d, approved_flag)

                    has_accept = any(
                        (isinstance(d, str) and d in ("accept", "cancel")) or
                        (isinstance(d, dict) and "acceptWithExecpolicyAmendment" in d)
                        for d in available_decisions
                    )

                    if rpc_id is not None:
                        # JSON-RPC request-style approval (must send response with same id)
                        if has_accept:
                            normalized_decision = _normalize_accept_decision(decision, bool(approved))
                        else:
                            normalized = _normalize_approved_decision(decision, bool(approved))
                            # Request-style API expects accept/cancel naming.
                            if normalized == "approved":
                                normalized_decision = "accept"
                            elif normalized == "abort":
                                normalized_decision = "cancel"
                            elif isinstance(normalized, dict):
                                amendment = _extract_amendment_from_decision(normalized)
                                normalized_decision = {
                                    "acceptWithExecpolicyAmendment": {
                                        "execpolicy_amendment": amendment or []
                                    }
                                }
                            else:
                                normalized_decision = "accept" if bool(approved) else "cancel"

                        # Newer app-server expects an object with explicit `decision`.
                        result_payload = {
                            "decision": normalized_decision,
                            "approved": bool(approved),
                        }

                        control_payload = {
                            "type": "rpc_response",
                            "rpc_id": rpc_id,
                            "result": result_payload
                        }
                    else:
                        # Notification-style approval
                        normalized = _normalize_decision_for_available(
                            decision,
                            bool(approved),
                            available_decisions,
                        )
                        params_payload = {
                            "call_id": call_id,
                            "callId": call_id,
                            "approved": bool(approved),
                        }
                        # Always include explicit decision token/object for compatibility.
                        if normalized == "approved":
                            params_payload["approved"] = True
                            params_payload["decision"] = "approved"
                        elif normalized == "abort":
                            params_payload["approved"] = False
                            params_payload["decision"] = "abort"
                        elif normalized == "accept":
                            params_payload["approved"] = True
                            params_payload["decision"] = "accept"
                        elif normalized == "cancel":
                            params_payload["approved"] = False
                            params_payload["decision"] = "cancel"
                        elif isinstance(normalized, dict):
                            params_payload.update(normalized)
                            params_payload["approved"] = True
                            params_payload["decision"] = normalized

                        control_payload = {
                            "method": "exec/approvalResponse",
                            "params": params_payload,
                        }

                    is_synthetic_request = (
                        meta.get("approval_method") == "item/commandExecution/requestApproval"
                        and rpc_id is None
                    )

                    if is_synthetic_request:
                        # Synthetic approvals are emitted by worker-side bridging when
                        # app-server produced a function_call without native approval RPC.
                        # Execute command directly once approved so the action is visible.
                        if bool(approved):
                            threading.Thread(
                                target=execute_synthetic_approved_command,
                                args=(dict(meta), str(job_id), call_id),
                                daemon=True,
                            ).start()
                    else:
                        provider.send_control(
                            job_id,
                            meta["node_id"],
                            control_payload,
                        )

                    if rpc_id is None:
                        PENDING_APPROVAL_DECISIONS[key] = {
                            "approved": bool(approved),
                            "decision": decision,
                            "timestamp": time.time(),
                        }
                    else:
                        PENDING_APPROVAL_DECISIONS.pop(key, None)
                except Exception as e:
                    emit_event("command_rejected", {
                        "request_id": request_id,
                        "reason": str(e)
                    })
                    continue

                del PENDING_APPROVALS[key]

                emit_event("exec_approval_resolved", {
                    "request_id": request_id,
                    "job_id": job_id,
                    "call_id": call_id,
                    "approved": approved,
                    "decision": decision,
                })

            elif command == "swarm_terminate":
                swarm_id = payload.get("swarm_id")
                swarm = SWARMS.get(swarm_id)
                if swarm:
                    job_id = swarm.get("job_id")

                    try:
                        provider.terminate(job_id)
                    except Exception as e:
                        emit_event("command_rejected", {
                            "request_id": request_id,
                            "reason": str(e)
                        })
                        continue

                    # Mark swarm terminated (do not delete immediately)
                    if swarm.get("status") != "terminated":
                        swarm["status"] = "terminated"
                        swarm["terminated_at"] = time.time()
                        save_state()
                    with SCHEDULER_LOCK:
                        node_count = int(swarm.get("node_count") or 0)
                        for node_id in range(node_count):
                            key = _node_key(swarm_id, node_id)
                            NODE_THREAD_ACTIVE.pop(key, None)
                            NODE_OUTSTANDING.pop(key, None)
                    with SCHEDULER_LOCK:
                        node_count = int(swarm.get("node_count") or 0)
                        for node_id in range(node_count):
                            key = _node_key(swarm_id, node_id)
                            NODE_THREAD_ACTIVE.pop(key, None)
                            NODE_OUTSTANDING.pop(key, None)

                    # Provider-specific archival (best-effort)
                    try:
                        provider.archive(job_id, swarm_id)
                    except Exception:
                        pass

                    emit_event("swarm_terminated", {
                        "request_id": request_id,
                        "swarm_id": swarm_id
                    })
                    with SCHEDULER_LOCK:
                        dropped = list(INTER_SWARM_QUEUE.pop(str(swarm_id), []))
                    for item in dropped:
                        emit_event("inter_swarm_dropped", {
                            "queue_id": item.get("queue_id"),
                            "request_id": item.get("request_id"),
                            "source_swarm_id": item.get("source_swarm_id"),
                            "target_swarm_id": swarm_id,
                            "reason": "target swarm terminated",
                        })
                    _emit_queue_updated()
# ================================
# Main
# ================================

def main():
    # Python version advisory (non-blocking)
    import sys
    min_version = (3, 11)
    if sys.version_info < min_version:
        print(
            f"[warning] Codeswarm is developed against Python {min_version[0]}.{min_version[1]}+; "
            f"detected {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}. "
            "It may work, but this version is not tested.",
            file=sys.stderr,
        )

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

    # Migration: ensure terminated swarms have terminated_at (for legacy entries)
    for swarm in SWARMS.values():
        if swarm.get("status") == "terminated" and "terminated_at" not in swarm:
            swarm["terminated_at"] = time.time()
    save_state()

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
