#!/usr/bin/env python3
import os
import json
import subprocess
import time
import select
import signal
import shlex
import errno
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


def parse_runtime_args(var_name, default_args):
    raw = os.environ.get(var_name)
    if not isinstance(raw, str) or not raw.strip():
        return list(default_args)

    try:
        parsed = json.loads(raw)
    except Exception as exc:
        raise RuntimeError(f"{var_name} must be valid JSON: {exc}") from exc

    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise RuntimeError(f"{var_name} must decode to a JSON array of strings.")

    return list(parsed)


def build_runtime_candidates(codex_home: str):
    codex_bin = os.environ.get("CODESWARM_CODEX_BIN", "codex")
    claude_bin = os.environ.get("CODESWARM_CLAUDE_BIN", "claude")

    codex_args = parse_runtime_args(
        "CODESWARM_CODEX_ARGS_JSON",
        [
            "--sandbox", "workspace-write",
            "--ask-for-approval", "never",
            "--add-dir", codex_home,
            "app-server",
            "--listen", "stdio://"
        ],
    )
    claude_args = parse_runtime_args(
        "CODESWARM_CLAUDE_ARGS_JSON",
        [
            "app-server",
            "--listen", "stdio://"
        ],
    )

    candidates = [
        {
            "runtime": "codex",
            "bin": str(codex_bin),
            "args": codex_args,
        }
    ]

    disable_claude_fallback = str(os.environ.get("CODESWARM_DISABLE_CLAUDE_FALLBACK", "")).lower() in {
        "1", "true", "yes"
    }
    if not disable_claude_fallback:
        candidates.append({
            "runtime": "claude",
            "bin": str(claude_bin),
            "args": claude_args,
        })

    return candidates


def launch_runtime(candidates, outbox, job_id, node_id):
    failures = []
    first_runtime = candidates[0]["runtime"] if candidates else None

    for candidate in candidates:
        command = [candidate["bin"], *candidate["args"]]
        try:
            proc = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1
            )
        except FileNotFoundError as exc:
            failures.append({
                "runtime": candidate["runtime"],
                "reason": f"not found: {exc}",
            })
            continue
        except OSError as exc:
            if exc.errno == errno.ENOEXEC:
                shell_command = ["/bin/bash", candidate["bin"], *candidate["args"]]
                try:
                    proc = subprocess.Popen(
                        shell_command,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        bufsize=1
                    )
                    command = shell_command
                except OSError as shell_exc:
                    failures.append({
                        "runtime": candidate["runtime"],
                        "reason": f"spawn failed: {shell_exc}",
                    })
                    continue
            else:
                failures.append({
                    "runtime": candidate["runtime"],
                    "reason": f"spawn failed: {exc}",
                })
                continue

        event = {
            "type": "worker_runtime_selected",
            "job_id": job_id,
            "node_id": node_id,
            "runtime": candidate["runtime"],
            "command": command,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if failures and first_runtime and candidate["runtime"] != first_runtime:
            event["fallback_from"] = first_runtime
            event["fallback_reasons"] = failures
        write_event(outbox, event)
        return proc, candidate["runtime"], command

    raise RuntimeError(
        "Unable to launch agent runtime. " +
        "; ".join(
            f"{failure['runtime']}: {failure['reason']}"
            for failure in failures
        )
    )


def finalize_worker(outbox, outbox_path, base, job_id, node_id, proc=None):
    write_event(outbox, {
        "type": "complete",
        "job_id": job_id,
        "node_id": node_id,
        "timestamp": datetime.now(timezone.utc).isoformat()
    })

    try:
        archive_dir = base / "mailbox" / "archive"
        archive_dir.mkdir(parents=True, exist_ok=True)

        archived_path = archive_dir / outbox_path.name
        outbox.flush()
        outbox.close()
        outbox_path.rename(archived_path)
    except Exception:
        pass

    if proc is not None:
        try:
            proc.terminate()
        except Exception:
            pass


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

    codex_home = str(Path.home() / ".codex")

    rpc_id = 0
    thread_id = None
    session_path = None
    shutdown_requested = False
    last_injection_id = None
    turn_to_injection = {}
    request_to_injection = {}
    pending_injections = deque()
    session_offset = 0
    emitted_escalation_calls = set()

    def handle_shutdown(signum, frame):
        nonlocal shutdown_requested
        shutdown_requested = True

    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)

    def _extract_turn_id(payload):
        params = payload.get("params", {}) if isinstance(payload, dict) else {}
        if isinstance(params, dict):
            # Many codex/event/* messages encode turn id in params.id.
            if isinstance(params.get("id"), str):
                return params.get("id")
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

    def emit_escalation_request_from_function_call(call_id, args, injection_id):
        if not call_id or call_id in emitted_escalation_calls:
            return

        emitted_escalation_calls.add(call_id)

        command = args.get("cmd")
        reason = args.get("justification")
        cwd = args.get("workdir") or os.getcwd()

        synthetic_payload = {
            "method": "item/commandExecution/requestApproval",
            "params": {
                "itemId": call_id,
                "command": command,
                "reason": reason,
                "cwd": cwd,
                "availableDecisions": ["accept", "cancel"],
            },
        }

        write_event(outbox, {
            "type": "codex_rpc",
            "job_id": job_id,
            "node_id": node_id,
            "injection_id": injection_id,
            "payload": synthetic_payload,
        })

    with open(outbox_path, "a", buffering=1) as outbox:
        try:
            runtime_candidates = build_runtime_candidates(codex_home)
            proc, runtime_name, runtime_command = launch_runtime(
                runtime_candidates,
                outbox,
                job_id,
                node_id,
            )
        except Exception as exc:
            write_event(outbox, {
                "type": "worker_error",
                "job_id": job_id,
                "node_id": node_id,
                "error": f"runtime_launch_failed: {exc}",
            })
            finalize_worker(outbox, outbox_path, base, job_id, node_id)
            return

        def send_request(method, params=None):
            nonlocal rpc_id
            request_id = rpc_id
            msg = jsonrpc_request(request_id, method, params)
            if proc.stdin is None:
                raise RuntimeError("runtime stdin unavailable")
            try:
                proc.stdin.write(msg + "\n")
                proc.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise RuntimeError(f"runtime request failed: {exc}") from exc
            rpc_id += 1
            return request_id

        def send_notification(method, params=None):
            msg = jsonrpc_notification(method, params)
            if proc.stdin is None:
                raise RuntimeError("runtime stdin unavailable")
            try:
                proc.stdin.write(msg + "\n")
                proc.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise RuntimeError(f"runtime notification failed: {exc}") from exc

        def send_response(id_, result=None):
            msg = {
                "jsonrpc": "2.0",
                "id": id_
            }
            if result is not None:
                msg["result"] = result
            if proc.stdin is None:
                raise RuntimeError("runtime stdin unavailable")
            try:
                proc.stdin.write(json.dumps(msg) + "\n")
                proc.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise RuntimeError(f"runtime response failed: {exc}") from exc

        write_event(outbox, {
            "type": "start",
            "job_id": job_id,
            "node_id": node_id,
            "hostname": hostname,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "runtime": runtime_name,
            "command": " ".join(shlex.quote(part) for part in runtime_command),
        })

        # LSP-style handshake
        try:
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
        except RuntimeError as exc:
            write_event(outbox, {
                "type": "worker_error",
                "job_id": job_id,
                "node_id": node_id,
                "error": str(exc),
            })
            finalize_worker(outbox, outbox_path, base, job_id, node_id, proc=proc)
            return

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

                        # Capture thread id/session path from thread/start response
                        if msg.get("result") and isinstance(msg.get("result"), dict):
                            result = msg["result"]
                            if isinstance(result.get("thread"), dict) and "id" in result["thread"]:
                                thread_id = result["thread"]["id"]
                            if isinstance(result.get("thread"), dict) and "path" in result["thread"]:
                                thread_path = result["thread"]["path"]
                                if isinstance(thread_path, str) and thread_path:
                                    session_path = Path(thread_path)
                                    session_offset = 0
                        # Fallback: some app-server flows emit thread status before/without thread/start response.
                        if not thread_id and msg.get("method") == "thread/status/changed":
                            params = msg.get("params")
                            if isinstance(params, dict) and isinstance(params.get("threadId"), str):
                                thread_id = params["threadId"]

                    except Exception as e:
                        write_event(outbox, {
                            "type": "worker_error",
                            "job_id": job_id,
                            "node_id": node_id,
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
                        try:
                            req_id = send_request("turn/start", {
                                "threadId": thread_id,
                                "input": [
                                    {
                                        "type": "text",
                                        "text": event.get("content", "")
                                    }
                                ]
                            })
                        except RuntimeError as exc:
                            write_event(outbox, {
                                "type": "worker_error",
                                "job_id": job_id,
                                "node_id": node_id,
                                "error": str(exc),
                            })
                            running = False
                            break
                        request_to_injection[req_id] = injection_id
                        pending_injections.append(injection_id)

                    elif event.get("type") == "control":
                        payload = event.get("payload", {})

                        if payload.get("type") == "rpc_response":
                            response_id = payload.get("rpc_id")
                            result = payload.get("result")
                            # Preserve local request id counter type; inbox rpc ids can be
                            # int or string depending on upstream request shape.
                            try:
                                send_response(response_id, result)
                            except RuntimeError as exc:
                                write_event(outbox, {
                                    "type": "worker_error",
                                    "job_id": job_id,
                                    "node_id": node_id,
                                    "error": str(exc),
                                })
                                running = False
                                break
                        else:
                            method = payload.get("method")
                            params = payload.get("params")

                            if method:
                                # Forward control notification directly to Codex app-server
                                try:
                                    send_notification(method, params)
                                except RuntimeError as exc:
                                    write_event(outbox, {
                                        "type": "worker_error",
                                        "job_id": job_id,
                                        "node_id": node_id,
                                        "error": str(exc),
                                    })
                                    running = False
                                    break

                    elif event.get("type") == "shutdown":
                        running = False
                        break

                inbox_offset += len(new_lines)

            # --- Session file tailing for function-call escalation requests ---
            if session_path and session_path.exists():
                try:
                    session_lines = session_path.read_text().splitlines()
                    new_session_lines = session_lines[session_offset:]
                    session_offset += len(new_session_lines)

                    for line in new_session_lines:
                        try:
                            entry = json.loads(line)
                        except Exception:
                            continue

                        if entry.get("type") != "response_item":
                            continue

                        payload = entry.get("payload", {})
                        if payload.get("type") != "function_call":
                            continue

                        if payload.get("name") != "exec_command":
                            continue

                        call_id = payload.get("call_id")
                        arguments_raw = payload.get("arguments")
                        if not isinstance(arguments_raw, str):
                            continue

                        try:
                            arguments = json.loads(arguments_raw)
                        except Exception:
                            continue

                        if not isinstance(arguments, dict):
                            continue

                        if arguments.get("sandbox_permissions") != "require_escalated":
                            continue

                        emit_escalation_request_from_function_call(
                            call_id,
                            arguments,
                            last_injection_id,
                        )
                except Exception as e:
                    write_event(outbox, {
                        "type": "worker_error",
                        "job_id": job_id,
                        "node_id": node_id,
                        "error": f"session_tail_error: {str(e)}"
                    })

            if proc.poll() is not None:
                running = False

        finalize_worker(outbox, outbox_path, base, job_id, node_id, proc=proc)


if __name__ == "__main__":
    main()
