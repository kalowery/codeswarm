#!/usr/bin/env python3
import os
import json
import subprocess
import time
import select
import signal
from collections import deque
from pathlib import Path
from datetime import datetime, timezone


def write_event(f, obj):
    f.write(json.dumps(obj) + "\n")
    f.flush()


def jsonrpc_request(id_, method, params=None):
    msg = {
        "jsonrpc": "2.0",
        "id": id_,
        "method": method
    }
    if params is not None:
        msg["params"] = params
    return json.dumps(msg)


def jsonrpc_notification(method, params=None):
    msg = {
        "jsonrpc": "2.0",
        "method": method
    }
    if params is not None:
        msg["params"] = params
    return json.dumps(msg)


def main():
    # Ensure worker runs from project root
    config = load_config_from_env() if 'load_config_from_env' in globals() else None
    try:
        if config:
            workspace_root = config["cluster"]["workspace_root"]
            cluster_subdir = config["cluster"]["cluster_subdir"]
            project_root = os.path.join(workspace_root, cluster_subdir)
            os.chdir(project_root)
    except Exception:
        pass


    job_id = os.environ["CODESWARM_JOB_ID"]
    node_id = int(os.environ["CODESWARM_NODE_ID"])
    hostname = os.uname().nodename

    base = Path(os.environ["CODESWARM_BASE_DIR"])

    inbox_path = base / "mailbox" / "inbox" / f"{job_id}_{node_id:02d}.jsonl"
    outbox_path = base / "mailbox" / "outbox" / f"{job_id}_{node_id:02d}.jsonl"

    inbox_path.parent.mkdir(parents=True, exist_ok=True)
    outbox_path.parent.mkdir(parents=True, exist_ok=True)

    codex_bin = os.environ.get("CODESWARM_CODEX_BIN", "codex")

    codex_home = str(Path.home() / ".codex")

    proc = subprocess.Popen(
        [
            str(codex_bin),
            "--sandbox", "workspace-write",
            "--ask-for-approval", "never",
            "--add-dir", codex_home,
            "app-server",
            "--listen", "stdio://"
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1
    )

    rpc_id = 0
    thread_id = None
    shutdown_requested = False
    last_injection_id = None
    turn_to_injection = {}
    request_to_injection = {}
    pending_injections = deque()

    def handle_shutdown(signum, frame):
        nonlocal shutdown_requested
        shutdown_requested = True

    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)

    def send_request(method, params=None):
        nonlocal rpc_id
        request_id = rpc_id
        msg = jsonrpc_request(request_id, method, params)
        proc.stdin.write(msg + "\n")
        proc.stdin.flush()
        rpc_id += 1
        return request_id

    def send_notification(method, params=None):
        msg = jsonrpc_notification(method, params)
        proc.stdin.write(msg + "\n")
        proc.stdin.flush()

    def send_response(id_, result=None):
        msg = {
            "jsonrpc": "2.0",
            "id": id_
        }
        if result is not None:
            msg["result"] = result
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()

    def _extract_turn_id(payload):
        params = payload.get("params", {}) if isinstance(payload, dict) else {}
        if isinstance(params, dict):
            if isinstance(params.get("turnId"), str):
                return params.get("turnId")
            turn = params.get("turn")
            if isinstance(turn, dict) and isinstance(turn.get("id"), str):
                return turn.get("id")
            msg = params.get("msg")
            if isinstance(msg, dict):
                if isinstance(msg.get("turn_id"), str):
                    return msg.get("turn_id")
                if isinstance(msg.get("turnId"), str):
                    return msg.get("turnId")
        result = payload.get("result", {}) if isinstance(payload, dict) else {}
        if isinstance(result, dict):
            turn = result.get("turn")
            if isinstance(turn, dict) and isinstance(turn.get("id"), str):
                return turn.get("id")
        return None

    def _resolve_injection_id(payload):
        msg_id = payload.get("id")
        if isinstance(msg_id, int) and msg_id in request_to_injection:
            return request_to_injection[msg_id]

        turn_id = _extract_turn_id(payload)
        if turn_id and turn_id in turn_to_injection:
            return turn_to_injection[turn_id]

        return last_injection_id

    with open(outbox_path, "a", buffering=1) as outbox:

        write_event(outbox, {
            "type": "start",
            "job_id": job_id,
            "node_id": node_id,
            "hostname": hostname,
            "timestamp": datetime.now(timezone.utc).isoformat()
        })

        # LSP-style handshake
        send_request("initialize", {
            "processId": None,
            "rootUri": None,
            "capabilities": {},
            "clientInfo": {
                "name": "codeswarm-worker",
                "title": "Codeswarm Worker",
                "version": "0.1.0"
            }
        })

        initialized_sent = False
        inbox_offset = 0
        running = True

        while running and not shutdown_requested:

            # ---- STDOUT (JSON-RPC) ----
            ready_out, _, _ = select.select([proc.stdout], [], [], 0.1)
            if ready_out:
                line = proc.stdout.readline()
                if line:
                    try:
                        msg = json.loads(line)
                        turn_id = _extract_turn_id(msg)
                        msg_id = msg.get("id")

                        # Bind turn_id -> injection_id from turn/start responses.
                        if (
                            isinstance(msg_id, int)
                            and msg_id in request_to_injection
                            and isinstance(msg.get("result"), dict)
                            and isinstance(msg["result"].get("turn"), dict)
                            and isinstance(msg["result"]["turn"].get("id"), str)
                        ):
                            resolved_injection = request_to_injection[msg_id]
                            turn_to_injection[msg["result"]["turn"]["id"]] = resolved_injection
                            del request_to_injection[msg_id]

                        # Fallback: bind from turn/started notifications if still pending.
                        if (
                            turn_id
                            and turn_id not in turn_to_injection
                            and pending_injections
                            and msg.get("method") == "turn/started"
                        ):
                            turn_to_injection[turn_id] = pending_injections.popleft()

                        resolved_injection_id = _resolve_injection_id(msg)

                        write_event(outbox, {
                            "type": "codex_rpc",
                            "job_id": job_id,
                            "node_id": node_id,
                            "injection_id": resolved_injection_id,
                            "payload": msg
                        })

                        # After initialize response
                        if msg.get("id") == 0 and not initialized_sent:
                            send_notification("initialized", {})
                            initialized_sent = True
                            send_request("thread/start", {})
                            continue

                        # Capture thread id from thread/start response
                        if msg.get("result") and isinstance(msg.get("result"), dict):
                            result = msg["result"]
                            if isinstance(result.get("thread"), dict) and "id" in result["thread"]:
                                thread_id = result["thread"]["id"]
                        # Fallback: some app-server flows emit thread status before/without thread/start response.
                        if not thread_id and msg.get("method") == "thread/status/changed":
                            params = msg.get("params")
                            if isinstance(params, dict) and isinstance(params.get("threadId"), str):
                                thread_id = params["threadId"]

                    except Exception as e:
                        write_event(outbox, {
                            "type": "worker_error",
                            "error": str(e)
                        })

            # ---- STDERR ----
            ready_err, _, _ = select.select([proc.stderr], [], [], 0.0)
            if ready_err:
                err_line = proc.stderr.readline()
                if err_line:
                    write_event(outbox, {
                        "type": "codex_stderr",
                        "job_id": job_id,
                        "node_id": node_id,
                        "line": err_line.strip()
                    })

            # ---- Inbox handling ----
            if inbox_path.exists() and thread_id:
                lines = inbox_path.read_text().splitlines()
                new_lines = lines[inbox_offset:]

                for line in new_lines:
                    try:
                        event = json.loads(line)
                    except:
                        continue

                    if event.get("type") == "user":
                        injection_id = event.get("injection_id")
                        last_injection_id = injection_id
                        req_id = send_request("turn/start", {
                            "threadId": thread_id,
                            "input": [
                                {
                                    "type": "text",
                                    "text": event.get("content", "")
                                }
                            ]
                        })
                        request_to_injection[req_id] = injection_id
                        pending_injections.append(injection_id)

                    elif event.get("type") == "control":
                        payload = event.get("payload", {})

                        if payload.get("type") == "rpc_response":
                            rpc_id = payload.get("rpc_id")
                            result = payload.get("result")
                            send_response(rpc_id, result)
                        else:
                            method = payload.get("method")
                            params = payload.get("params")

                            if method:
                                # Forward control notification directly to Codex app-server
                                send_notification(method, params)

                    elif event.get("type") == "shutdown":
                        running = False
                        break

                inbox_offset += len(new_lines)

            if proc.poll() is not None:
                running = False

        write_event(outbox, {
            "type": "complete",
            "job_id": job_id,
            "node_id": node_id,
            "timestamp": datetime.now(timezone.utc).isoformat()
        })

        # Archive outbox file after completion (transport lifecycle cleanup)
        try:
            archive_dir = base / "mailbox" / "archive"
            archive_dir.mkdir(parents=True, exist_ok=True)

            archived_path = archive_dir / outbox_path.name
            outbox.flush()
            outbox.close()
            outbox_path.rename(archived_path)
        except Exception as e:
            # Non-fatal; follower may continue until cleanup
            pass

        try:
            proc.terminate()
        except:
            pass


if __name__ == "__main__":
    main()
