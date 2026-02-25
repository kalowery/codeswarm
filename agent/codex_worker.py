#!/usr/bin/env python3
import os
import json
import subprocess
import time
import select
import signal
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
    job_id = os.environ["SLURM_JOB_ID"]
    node_id = int(os.environ["SLURM_NODEID"])
    hostname = os.uname().nodename

    workspace_root = os.environ["WORKSPACE_ROOT"]
    cluster_subdir = os.environ["CLUSTER_SUBDIR"]

    base = Path(workspace_root) / cluster_subdir

    inbox_path = base / "mailbox" / "inbox" / f"{job_id}_{node_id:02d}.jsonl"
    outbox_path = base / "mailbox" / "outbox" / f"{job_id}_{node_id:02d}.jsonl"

    inbox_path.parent.mkdir(parents=True, exist_ok=True)
    outbox_path.parent.mkdir(parents=True, exist_ok=True)

    codex_bin = base / "tools" / "npm-global" / "bin" / "codex"

    proc = subprocess.Popen(
        [
            str(codex_bin),
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

    def handle_shutdown(signum, frame):
        nonlocal shutdown_requested
        shutdown_requested = True

    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)

    def send_request(method, params=None):
        nonlocal rpc_id
        msg = jsonrpc_request(rpc_id, method, params)
        proc.stdin.write(msg + "\n")
        proc.stdin.flush()
        rpc_id += 1

    def send_notification(method, params=None):
        msg = jsonrpc_notification(method, params)
        proc.stdin.write(msg + "\n")
        proc.stdin.flush()

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
                        write_event(outbox, {
                            "type": "codex_rpc",
                            "job_id": job_id,
                            "node_id": node_id,
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
                        send_request("turn/start", {
                            "threadId": thread_id,
                            "input": [
                                {
                                    "type": "text",
                                    "text": event.get("content", "")
                                }
                            ]
                        })

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
