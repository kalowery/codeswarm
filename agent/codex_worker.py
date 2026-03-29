#!/usr/bin/env python3
import os
import json
import subprocess
import time
import select
import signal
import tempfile
from collections import deque
from pathlib import Path
from datetime import datetime, timezone


CODEX_ROLLOUT_CHANNEL_CLOSED_MARKER = "failed to record rollout items: failed to queue rollout items: channel closed"
CODEX_MAX_RESTARTS = 5
CODEX_RESTART_BACKOFF_SECONDS = 1.0


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
    sandbox_mode = os.environ.get("CODESWARM_SANDBOX_MODE")
    approval_policy = os.environ.get("CODESWARM_ASK_FOR_APPROVAL")
    capture_all_session = os.environ.get("CODESWARM_CAPTURE_ALL_SESSION", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    native_auto_approve = os.environ.get("CODESWARM_NATIVE_AUTO_APPROVE", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    fresh_thread_per_injection = os.environ.get("CODESWARM_FRESH_THREAD_PER_INJECTION", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )

    codex_home = str(Path.home() / ".codex")
    workspace_dir = os.getcwd()
    workspace_root = Path(workspace_dir).resolve()
    stderr_log_path = Path(workspace_dir) / "codex.stderr.log"

    def launch_codex_proc():
        cmd = [
            str(codex_bin),
            "--cd", workspace_dir,
        ]
        if sandbox_mode and str(sandbox_mode).strip():
            cmd.extend(["--sandbox", str(sandbox_mode).strip()])
        if approval_policy and str(approval_policy).strip():
            cmd.extend(["--ask-for-approval", str(approval_policy).strip()])
        cmd.extend([
            "--add-dir", codex_home,
            "--add-dir", workspace_dir,
            "app-server",
            "--listen", "stdio://"
        ])
        return subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1
        )

    proc = launch_codex_proc()

    rpc_id = 0
    thread_id = None
    current_active_turn_id = None
    session_path = None
    shutdown_requested = False
    last_injection_id = None
    turn_to_injection = {}
    request_to_injection = {}
    pending_user_requests = {}
    pending_injections = deque()
    session_offset = 0
    pending_dynamic_tool_requests = {}
    pending_dynamic_tool_calls = {}
    pending_session_tool_calls = {}
    native_items = {}
    session_response_history = []
    session_trace_lines_remaining = 0
    session_trace_reason = None
    session_trace_turn_id = None
    persistent_preamble = None
    pending_fresh_turn = None
    pending_fresh_thread_request_id = None
    SESSION_TOOL_NATIVE_GRACE_SECONDS = 1.0
    restart_count = 0
    last_restart_ts = 0.0
    rehydrate_history_on_thread_start = None

    def handle_shutdown(signum, frame):
        nonlocal shutdown_requested
        shutdown_requested = True

    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)

    def send_request(method, params=None):
        nonlocal rpc_id, proc
        request_id = rpc_id
        msg = jsonrpc_request(request_id, method, params)
        proc.stdin.write(msg + "\n")
        proc.stdin.flush()
        rpc_id += 1
        return request_id

    def _send_user_turn_start(injection_id, content):
        req_id = send_request("turn/start", {
            "threadId": thread_id,
            "input": [
                {
                    "type": "text",
                    "text": content,
                }
            ],
        })
        request_to_injection[req_id] = injection_id
        pending_injections.append(injection_id)
        pending_user_requests[req_id] = {
            "kind": "turn_start",
            "injection_id": injection_id,
            "content": content,
        }
        return req_id

    def _send_user_turn_steer(injection_id, content):
        req_id = send_request("turn/steer", {
            "threadId": thread_id,
            "expectedTurnId": current_active_turn_id,
            "input": [
                {
                    "type": "text",
                    "text": content,
                }
            ],
        })
        request_to_injection[req_id] = injection_id
        if current_active_turn_id:
            # Rebind the active turn so streamed output from the steered turn
            # is attributed to the latest injection instead of the bootstrap one.
            turn_to_injection[current_active_turn_id] = injection_id
        pending_user_requests[req_id] = {
            "kind": "turn_steer",
            "injection_id": injection_id,
            "content": content,
            "expected_turn_id": current_active_turn_id,
        }
        return req_id

    def _start_fresh_turn(injection_id, content):
        nonlocal pending_fresh_turn, pending_fresh_thread_request_id
        pending_fresh_turn = {
            "injection_id": injection_id,
            "content": content,
        }
        pending_fresh_thread_request_id = send_request("thread/start", {})
        return pending_fresh_thread_request_id

    def send_notification(method, params=None):
        nonlocal proc
        msg = jsonrpc_notification(method, params)
        proc.stdin.write(msg + "\n")
        proc.stdin.flush()

    def send_response(id_, result=None):
        nonlocal proc
        msg = {
            "jsonrpc": "2.0",
            "id": id_
        }
        if result is not None:
            msg["result"] = result
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()

    def _format_exec_output(command, completed):
        parts = [f"Command: {command}"]
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        if stdout:
            parts.append(stdout.rstrip())
        if stderr:
            parts.append(stderr.rstrip())
        if completed.returncode not in (0, None):
            parts.append(f"Exit code: {completed.returncode}")
        output = "\n".join(part for part in parts if part)
        return output[:16000]

    def _execute_exec_command_args(args):
        command = args.get("cmd")
        if not isinstance(command, str) or not command.strip():
            raise RuntimeError("exec_command args missing non-empty cmd")

        shell_bin = args.get("shell") or os.environ.get("SHELL") or "/bin/bash"
        shell_flag = "-lc" if bool(args.get("login", True)) else "-c"
        cwd = args.get("workdir") or os.getcwd()
        env = os.environ.copy()
        override_env = args.get("env")
        if isinstance(override_env, dict):
            for key, value in override_env.items():
                if value is None:
                    env.pop(str(key), None)
                else:
                    env[str(key)] = str(value)

        return subprocess.run(
            [str(shell_bin), shell_flag, command],
            cwd=cwd,
            env=env,
            text=True,
            capture_output=True,
        )

    def _apply_patch_text(patch_text):
        if not isinstance(patch_text, str) or not patch_text.strip():
            raise RuntimeError("apply_patch input missing")

        def _resolve_workspace_path(raw_path):
            if not isinstance(raw_path, str) or not raw_path.strip():
                raise RuntimeError("invalid patch path")
            path = Path(raw_path).expanduser()
            if not path.is_absolute():
                path = (Path(workspace_dir) / path).resolve()
            else:
                path = path.resolve()
            workspace_root = Path(workspace_dir).resolve()
            if path != workspace_root and workspace_root not in path.parents:
                raise RuntimeError(f"path outside workspace: {path}")
            return path

        def _write_text(path, content):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

        def _find_subsequence(haystack, needle, start):
            if not needle:
                return start
            end = len(haystack) - len(needle) + 1
            for idx in range(max(0, start), max(0, end)):
                if haystack[idx:idx + len(needle)] == needle:
                    return idx
            return -1

        def _apply_update_file(path, patch_lines, move_to=None):
            original_text = path.read_text(encoding="utf-8")
            had_trailing_newline = original_text.endswith("\n")
            original_lines = original_text.splitlines()
            result = []
            src_idx = 0
            i = 0
            while i < len(patch_lines):
                marker = patch_lines[i]
                if not marker.startswith("@@"):
                    i += 1
                    continue
                i += 1
                chunk = []
                while i < len(patch_lines) and not patch_lines[i].startswith("@@"):
                    chunk.append(patch_lines[i])
                    i += 1
                old_seq = []
                for entry in chunk:
                    if entry.startswith((" ", "-")):
                        old_seq.append(entry[1:])
                pos = _find_subsequence(original_lines, old_seq, src_idx)
                if pos < 0:
                    raise RuntimeError(f"failed to locate patch context for {path}")
                result.extend(original_lines[src_idx:pos])
                cur = pos
                for entry in chunk:
                    prefix = entry[:1]
                    text = entry[1:]
                    if prefix == " ":
                        if cur >= len(original_lines) or original_lines[cur] != text:
                            raise RuntimeError(f"context mismatch while patching {path}")
                        result.append(text)
                        cur += 1
                    elif prefix == "-":
                        if cur >= len(original_lines) or original_lines[cur] != text:
                            raise RuntimeError(f"delete mismatch while patching {path}")
                        cur += 1
                    elif prefix == "+":
                        result.append(text)
                    else:
                        raise RuntimeError(f"unsupported patch line: {entry}")
                src_idx = cur
            result.extend(original_lines[src_idx:])
            new_text = "\n".join(result)
            if had_trailing_newline or new_text:
                new_text += "\n"
            target_path = _resolve_workspace_path(move_to) if move_to else path
            if move_to and target_path != path:
                target_path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            _write_text(target_path, new_text)
            return target_path

        lines = patch_text.splitlines()
        if not lines or lines[0] != "*** Begin Patch":
            raise RuntimeError("invalid patch header")
        i = 1
        changed = []
        while i < len(lines):
            line = lines[i]
            if line == "*** End Patch":
                break
            if line.startswith("*** Add File: "):
                target = _resolve_workspace_path(line[len("*** Add File: "):])
                i += 1
                content_lines = []
                while i < len(lines) and not lines[i].startswith("*** "):
                    entry = lines[i]
                    if not entry.startswith("+"):
                        raise RuntimeError(f"invalid add line: {entry}")
                    content_lines.append(entry[1:])
                    i += 1
                content = "\n".join(content_lines)
                if content:
                    content += "\n"
                _write_text(target, content)
                changed.append(str(target))
                continue
            if line.startswith("*** Delete File: "):
                target = _resolve_workspace_path(line[len("*** Delete File: "):])
                try:
                    target.unlink()
                except FileNotFoundError:
                    pass
                changed.append(str(target))
                i += 1
                continue
            if line.startswith("*** Update File: "):
                target = _resolve_workspace_path(line[len("*** Update File: "):])
                i += 1
                move_to = None
                if i < len(lines) and lines[i].startswith("*** Move to: "):
                    move_to = lines[i][len("*** Move to: "):]
                    i += 1
                patch_lines = []
                while i < len(lines) and not lines[i].startswith("*** "):
                    patch_lines.append(lines[i])
                    i += 1
                changed.append(str(_apply_update_file(target, patch_lines, move_to=move_to)))
                continue
            raise RuntimeError(f"unsupported patch block: {line}")
        return changed

    def _session_output_item(kind, call_id, success, text):
        del success
        return {
            "type": kind,
            "call_id": call_id,
            "output": text[:16000],
        }

    def _resume_thread_with_output(output_item, injection_id):
        if not thread_id:
            raise RuntimeError("thread_id unavailable for thread/resume")
        history = list(session_response_history)
        history.append(output_item)
        req_id = send_request("thread/resume", {
            "threadId": thread_id,
            "history": history,
            "persistExtendedHistory": True,
        })
        if injection_id is not None:
            request_to_injection[req_id] = injection_id
            pending_injections.append(injection_id)

    def _finish_session_tool_call(call_id):
        pending_session_tool_calls.pop(call_id, None)

    def _mark_native_session_tool_call(call_id):
        info = pending_session_tool_calls.get(call_id)
        if not isinstance(info, dict):
            return
        info["state"] = "native"
        info["native_seen"] = True

    def _fulfill_session_tool_call(call_id, approved):
        info = pending_session_tool_calls.get(call_id)
        if not info or info.get("state") in ("done", "running", "native"):
            return
        info["state"] = "running"
        tool_type = info.get("tool_type")
        injection_id = info.get("injection_id")
        try:
            if not approved:
                if tool_type == "function_call_output":
                    text = "Command execution was denied by approval policy."
                else:
                    text = "Patch application was denied by approval policy."
                output_item = _session_output_item(tool_type, call_id, False, text)
                _resume_thread_with_output(output_item, injection_id)
                write_event(outbox, {
                    "type": "worker_trace",
                    "job_id": job_id,
                    "node_id": node_id,
                    "injection_id": injection_id,
                    "event": "session_tool_denied",
                    "call_id": call_id,
                    "tool_type": tool_type,
                })
            elif tool_type == "function_call_output":
                args = info.get("arguments") or {}
                completed = _execute_exec_command_args(args)
                text = _format_exec_output(args.get("cmd"), completed)
                output_item = _session_output_item(tool_type, call_id, completed.returncode == 0, text)
                _resume_thread_with_output(output_item, injection_id)
                write_event(outbox, {
                    "type": "worker_trace",
                    "job_id": job_id,
                    "node_id": node_id,
                    "injection_id": injection_id,
                    "event": "session_tool_fulfilled",
                    "call_id": call_id,
                    "tool_type": tool_type,
                    "success": completed.returncode == 0,
                })
            elif tool_type == "custom_tool_call_output":
                changed = _apply_patch_text(info.get("input") or "")
                text = "Success. Updated the following files:\n" + "\n".join(
                    f"M {path}" for path in changed
                )
                output_item = _session_output_item(tool_type, call_id, True, text)
                _resume_thread_with_output(output_item, injection_id)
                write_event(outbox, {
                    "type": "worker_trace",
                    "job_id": job_id,
                    "node_id": node_id,
                    "injection_id": injection_id,
                    "event": "session_tool_fulfilled",
                    "call_id": call_id,
                    "tool_type": tool_type,
                    "success": True,
                })
            else:
                raise RuntimeError(f"unsupported session tool type: {tool_type}")
            info["state"] = "done"
        except Exception as exc:
            output_item = _session_output_item(
                tool_type,
                call_id,
                False,
                f"{tool_type} failed: {exc}",
            )
            try:
                _resume_thread_with_output(output_item, injection_id)
            except Exception:
                pass
            write_event(outbox, {
                "type": "worker_error",
                "job_id": job_id,
                "node_id": node_id,
                "injection_id": injection_id,
                "error": f"session_tool_fulfillment_error[{call_id}]: {exc}",
            })
            info["state"] = "done"

    def _finish_dynamic_tool_call(request_id, call_id):
        pending_dynamic_tool_requests.pop(request_id, None)
        if isinstance(call_id, str):
            pending_dynamic_tool_calls.pop(call_id, None)

    def _extract_approval_call_id_from_rpc(msg):
        if not isinstance(msg, dict):
            return None
        method = msg.get("method")
        if method not in (
            "item/fileChange/requestApproval",
            "item/commandExecution/requestApproval",
            "applyPatchApproval",
            "execCommandApproval",
            "codex/event/apply_patch_approval_request",
            "codex/event/exec_approval_request",
        ):
            return None
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        if not isinstance(params, dict):
            return None
        native_call_id = (
            params.get("itemId")
            or params.get("callId")
            or params.get("call_id")
        )
        return native_call_id if isinstance(native_call_id, str) else None

    def _path_within_workspace(raw_path):
        if not isinstance(raw_path, str) or not raw_path.strip():
            return False
        try:
            resolved = Path(raw_path).expanduser().resolve(strict=False)
        except Exception:
            return False
        return resolved == workspace_root or workspace_root in resolved.parents

    def _auto_native_approval_route(msg, injection_id):
        if not native_auto_approve or not isinstance(msg, dict):
            return False

        method = msg.get("method")
        if method not in (
            "item/fileChange/requestApproval",
            "item/commandExecution/requestApproval",
            "applyPatchApproval",
            "execCommandApproval",
            "codex/event/apply_patch_approval_request",
            "codex/event/exec_approval_request",
        ):
            return False

        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        request_id = msg.get("id")
        call_id = _extract_approval_call_id_from_rpc(msg)
        approved = False
        reason = None

        if method in ("item/fileChange/requestApproval", "applyPatchApproval", "codex/event/apply_patch_approval_request"):
            item = native_items.get(call_id) if isinstance(call_id, str) else None
            changes = item.get("changes") if isinstance(item, dict) else None
            changed_paths = []
            if isinstance(changes, list):
                for change in changes:
                    if isinstance(change, dict):
                        changed_paths.append(change.get("path"))
            grant_root = params.get("grantRoot")
            if changed_paths and all(_path_within_workspace(path) for path in changed_paths):
                approved = grant_root is None or _path_within_workspace(grant_root)
            else:
                reason = "file_change_outside_workspace_or_missing_paths"
            decision = "approved" if method in ("applyPatchApproval", "codex/event/apply_patch_approval_request") else "accept"
            denied_decision = "denied" if decision == "approved" else "cancel"
        else:
            command_cwd = params.get("cwd")
            grant_root = params.get("grantRoot")
            proposed_execpolicy_amendment = params.get("proposedExecPolicyAmendment")
            approved = _path_within_workspace(command_cwd) and not proposed_execpolicy_amendment
            if approved and grant_root is not None:
                approved = _path_within_workspace(grant_root)
            if not approved:
                reason = "command_request_outside_workspace_or_requires_execpolicy_amendment"
            decision = "approved" if method == "execCommandApproval" else "accept"
            denied_decision = "denied" if decision == "approved" else "cancel"

        rpc_decision = decision if approved else denied_decision

        if isinstance(request_id, int):
            send_response(request_id, {
                "decision": rpc_decision,
            })
        if isinstance(call_id, str) and call_id:
            notify_params = {
                "call_id": call_id,
                "callId": call_id,
                "approved": bool(approved),
                "decision": rpc_decision,
            }
            turn_id = params.get("turnId") or params.get("turn_id") or params.get("id")
            if isinstance(turn_id, str) and turn_id:
                notify_params["id"] = turn_id
                notify_params["turn_id"] = turn_id
                notify_params["turnId"] = turn_id
            send_notification("exec/approvalResponse", notify_params)

        write_event(outbox, {
            "type": "worker_trace",
            "job_id": job_id,
            "node_id": node_id,
            "injection_id": injection_id,
            "event": "native_approval_auto_responded",
            "approval_method": method,
            "call_id": call_id,
            "request_id": request_id,
            "approved": bool(approved),
            "decision": rpc_decision,
            "reason": reason,
        })
        return True

    def restart_codex(reason, outbox):
        nonlocal proc, rpc_id, thread_id, current_active_turn_id, session_path, session_offset
        nonlocal request_to_injection, turn_to_injection, pending_injections, pending_user_requests
        nonlocal pending_dynamic_tool_requests, pending_dynamic_tool_calls
        nonlocal pending_session_tool_calls, native_items, initialized_sent
        nonlocal restart_count, last_restart_ts, rehydrate_history_on_thread_start
        nonlocal session_trace_lines_remaining, session_trace_reason, session_trace_turn_id
        nonlocal pending_fresh_turn, pending_fresh_thread_request_id

        if shutdown_requested:
            return False
        now = time.time()
        if restart_count >= CODEX_MAX_RESTARTS:
            write_event(outbox, {
                "type": "worker_error",
                "job_id": job_id,
                "node_id": node_id,
                "injection_id": last_injection_id,
                "error": f"codex_restart_limit_exceeded: {reason}",
            })
            return False
        if now - last_restart_ts < CODEX_RESTART_BACKOFF_SECONDS:
            time.sleep(CODEX_RESTART_BACKOFF_SECONDS - (now - last_restart_ts))

        restart_count += 1
        last_restart_ts = time.time()
        rehydrate_history_on_thread_start = None if fresh_thread_per_injection else list(session_response_history)
        write_event(outbox, {
            "type": "worker_trace",
            "job_id": job_id,
            "node_id": node_id,
            "injection_id": last_injection_id,
            "event": "codex_restart_requested",
            "reason": reason,
            "restart_count": restart_count,
            "rehydrate_items": len(rehydrate_history_on_thread_start),
        })
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except Exception:
                    proc.kill()
                    proc.wait(timeout=2)
        except Exception:
            pass

        proc = launch_codex_proc()
        rpc_id = 0
        thread_id = None
        session_path = None
        session_offset = 0
        request_to_injection = {}
        pending_user_requests = {}
        turn_to_injection = {}
        pending_injections = deque()
        current_active_turn_id = None
        pending_dynamic_tool_requests = {}
        pending_dynamic_tool_calls = {}
        pending_session_tool_calls = {}
        native_items = {}
        initialized_sent = False
        session_trace_lines_remaining = 0
        session_trace_reason = None
        session_trace_turn_id = None
        pending_fresh_turn = None
        pending_fresh_thread_request_id = None

        write_event(outbox, {
            "type": "worker_trace",
            "job_id": job_id,
            "node_id": node_id,
            "injection_id": last_injection_id,
            "event": "codex_restarted",
            "reason": reason,
            "restart_count": restart_count,
            "pid": proc.pid,
        })
        send_request("initialize", {
            "processId": None,
            "rootUri": None,
            "capabilities": {
                "experimentalApi": True,
            },
            "clientInfo": {
                "name": "codeswarm-worker",
                "title": "Codeswarm Worker",
                "version": "0.1.0"
            }
        })
        return True

    def _send_dynamic_tool_result(request_id, success, text, call_id):
        send_response(request_id, {
            "success": bool(success),
            "contentItems": [
                {
                    "type": "inputText",
                    "text": text[:16000],
                }
            ],
        })
        _finish_dynamic_tool_call(request_id, call_id)

    def _handle_dynamic_exec_approval(request_id, approved):
        info = pending_dynamic_tool_requests.get(request_id)
        if not info:
            return
        call_id = info.get("call_id")
        if not approved:
            _send_dynamic_tool_result(
                request_id,
                False,
                "Command execution was denied by approval policy.",
                call_id,
            )
            return

        try:
            completed = _execute_exec_command_args(info.get("arguments") or {})
            output = _format_exec_output((info.get("arguments") or {}).get("cmd"), completed)
            _send_dynamic_tool_result(
                request_id,
                completed.returncode == 0,
                output,
                call_id,
            )
        except Exception as exc:
            _send_dynamic_tool_result(
                request_id,
                False,
                f"exec_command failed: {exc}",
                call_id,
            )

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

    def _emit_dynamic_exec_approval_request(request_id, call_id, arguments, injection_id):
        write_event(outbox, {
            "type": "codex_rpc",
            "job_id": job_id,
            "node_id": node_id,
            "injection_id": injection_id,
            "payload": {
                "id": request_id,
                "method": "item/commandExecution/requestApproval",
                "params": {
                    "itemId": call_id,
                    "command": arguments.get("cmd"),
                    "reason": arguments.get("justification"),
                    "cwd": arguments.get("workdir") or os.getcwd(),
                    "availableDecisions": ["accept", "cancel"],
                    "synthetic_request": True,
                    "tool_name": "exec_command",
                },
            },
        })

    with open(outbox_path, "a", buffering=1) as outbox:
        stderr_log_path.write_text("", encoding="utf-8")

        write_event(outbox, {
            "type": "start",
            "job_id": job_id,
            "node_id": node_id,
            "hostname": hostname,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "capture_all_session": capture_all_session,
        })

        # LSP-style handshake
        send_request("initialize", {
            "processId": None,
            "rootUri": None,
            "capabilities": {
                "experimentalApi": True,
            },
            "clientInfo": {
                "name": "codeswarm-worker",
                "title": "Codeswarm Worker",
                "version": "0.1.0"
            }
        })

        initialized_sent = False
        inbox_offset_bytes = 0
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
                            current_turn_id = msg["result"]["turn"]["id"]
                            turn_to_injection[current_turn_id] = resolved_injection
                            current_active_turn_id = current_turn_id
                            del request_to_injection[msg_id]

                        # Fallback: bind from turn/started notifications if still pending.
                        if (
                            turn_id
                            and turn_id not in turn_to_injection
                            and pending_injections
                            and msg.get("method") == "turn/started"
                        ):
                            turn_to_injection[turn_id] = pending_injections.popleft()
                            current_active_turn_id = turn_id

                        if msg.get("method") == "turn/started" and turn_id:
                            current_active_turn_id = turn_id

                        if msg.get("method") == "turn/completed" and turn_id and turn_id == current_active_turn_id:
                            current_active_turn_id = None

                        if (
                            fresh_thread_per_injection
                            and isinstance(msg_id, int)
                            and msg_id == pending_fresh_thread_request_id
                        ):
                            if msg.get("error"):
                                write_event(outbox, {
                                    "type": "worker_error",
                                    "job_id": job_id,
                                    "node_id": node_id,
                                    "injection_id": (pending_fresh_turn or {}).get("injection_id"),
                                    "error": f"fresh_thread_start_failed: {msg.get('error')}",
                                })
                                pending_fresh_turn = None
                                pending_fresh_thread_request_id = None
                            elif pending_fresh_turn and isinstance(msg.get("result"), dict):
                                result_thread = msg["result"].get("thread")
                                if isinstance(result_thread, dict) and isinstance(result_thread.get("id"), str):
                                    thread_id = result_thread["id"]
                                if thread_id:
                                    _send_user_turn_start(
                                        pending_fresh_turn.get("injection_id"),
                                        pending_fresh_turn.get("content", ""),
                                    )
                                    write_event(outbox, {
                                        "type": "worker_trace",
                                        "job_id": job_id,
                                        "node_id": node_id,
                                        "injection_id": pending_fresh_turn.get("injection_id"),
                                        "event": "fresh_thread_started",
                                        "thread_id": thread_id,
                                    })
                                    pending_fresh_turn = None
                                    pending_fresh_thread_request_id = None

                        if isinstance(msg_id, int) and msg_id in pending_user_requests:
                            pending_request = pending_user_requests.pop(msg_id, None)
                            if msg.get("error") and pending_request and pending_request.get("kind") == "turn_steer":
                                fallback_injection_id = pending_request.get("injection_id")
                                fallback_content = pending_request.get("content", "")
                                write_event(outbox, {
                                    "type": "worker_trace",
                                    "job_id": job_id,
                                    "node_id": node_id,
                                    "injection_id": fallback_injection_id,
                                    "event": "turn_steer_failed_fallback_start",
                                    "error": msg.get("error"),
                                    "expected_turn_id": pending_request.get("expected_turn_id"),
                                })
                                _send_user_turn_start(fallback_injection_id, fallback_content)
                                continue

                        resolved_injection_id = _resolve_injection_id(msg)

                        write_event(outbox, {
                            "type": "codex_rpc",
                            "job_id": job_id,
                            "node_id": node_id,
                            "injection_id": resolved_injection_id,
                            "payload": msg
                        })

                        native_call_id = _extract_approval_call_id_from_rpc(msg)
                        if native_call_id:
                            _mark_native_session_tool_call(native_call_id)

                        if msg.get("method") == "thread/status/changed":
                            params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
                            status = params.get("status") if isinstance(params.get("status"), dict) else {}
                            active_flags = status.get("activeFlags") if isinstance(status.get("activeFlags"), list) else []
                            status_type = str(status.get("type") or "").lower()
                            if status_type in ("idle", "complete", "completed"):
                                current_active_turn_id = None
                            if "waitingOnApproval" in active_flags:
                                session_trace_lines_remaining = max(session_trace_lines_remaining, 120)
                                session_trace_reason = "waitingOnApproval"
                                session_trace_turn_id = _extract_turn_id(msg)
                                write_event(outbox, {
                                    "type": "worker_trace",
                                    "job_id": job_id,
                                    "node_id": node_id,
                                    "injection_id": resolved_injection_id,
                                    "event": "session_trace_armed",
                                    "reason": session_trace_reason,
                                    "turn_id": session_trace_turn_id,
                                })

                        if msg.get("method") in ("item/started", "item/completed"):
                            params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
                            item = params.get("item") if isinstance(params.get("item"), dict) else {}
                            item_id = item.get("id")
                            if isinstance(item_id, str) and item_id:
                                native_items[item_id] = item

                        if _auto_native_approval_route(msg, resolved_injection_id):
                            continue

                        if msg.get("method") == "item/tool/call":
                            params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
                            request_id = msg.get("id")
                            call_id = params.get("callId")
                            tool_name = params.get("tool")
                            arguments = params.get("arguments")
                            if (
                                isinstance(request_id, int)
                                and isinstance(call_id, str)
                                and tool_name == "exec_command"
                                and isinstance(arguments, dict)
                            ):
                                _mark_native_session_tool_call(call_id)
                                pending_dynamic_tool_requests[request_id] = {
                                    "call_id": call_id,
                                    "arguments": arguments,
                                    "injection_id": resolved_injection_id,
                                }
                                pending_dynamic_tool_calls[call_id] = request_id
                                if arguments.get("sandbox_permissions") == "require_escalated":
                                    _emit_dynamic_exec_approval_request(
                                        request_id,
                                        call_id,
                                        arguments,
                                        resolved_injection_id,
                                    )
                                else:
                                    _handle_dynamic_exec_approval(request_id, True)

                        # After initialize response
                        if msg.get("id") == 0 and not initialized_sent:
                            send_notification("initialized", {})
                            initialized_sent = True
                            if not fresh_thread_per_injection:
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
                            if (
                                thread_id
                                and rehydrate_history_on_thread_start is not None
                                and msg.get("id") != 0
                            ):
                                history = rehydrate_history_on_thread_start
                                rehydrate_history_on_thread_start = None
                                if history:
                                    send_request("thread/resume", {
                                        "threadId": thread_id,
                                        "history": history,
                                        "persistExtendedHistory": True,
                                    })
                                    write_event(outbox, {
                                        "type": "worker_trace",
                                        "job_id": job_id,
                                        "node_id": node_id,
                                        "injection_id": last_injection_id,
                                        "event": "codex_rehydrate_sent",
                                        "items": len(history),
                                    })
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
                    stripped_err = err_line.strip()
                    try:
                        with stderr_log_path.open("a", encoding="utf-8") as stderr_log:
                            stderr_log.write(stripped_err + "\n")
                    except Exception:
                        pass
                    if CODEX_ROLLOUT_CHANNEL_CLOSED_MARKER in stripped_err:
                        continue
                    write_event(outbox, {
                        "type": "codex_stderr",
                        "job_id": job_id,
                        "node_id": node_id,
                        "line": stripped_err
                    })

            # ---- Inbox handling ----
            if inbox_path.exists() and (thread_id or fresh_thread_per_injection):
                try:
                    inbox_size = inbox_path.stat().st_size
                    if inbox_offset_bytes > inbox_size:
                        # Inbox was truncated/rotated; restart tailing from beginning.
                        inbox_offset_bytes = 0

                    with inbox_path.open("rb") as inbox_file:
                        inbox_file.seek(inbox_offset_bytes)
                        while True:
                            line_start = inbox_file.tell()
                            raw_line = inbox_file.readline()
                            if not raw_line:
                                break
                            # If writer hasn't flushed a full JSONL record yet, do not
                            # consume this partial line; retry on next poll.
                            if not raw_line.endswith(b"\n"):
                                inbox_file.seek(line_start)
                                break
                            try:
                                line = raw_line.decode("utf-8").strip()
                            except Exception:
                                inbox_offset_bytes = inbox_file.tell()
                                continue
                            if not line:
                                inbox_offset_bytes = inbox_file.tell()
                                continue
                            try:
                                event = json.loads(line)
                            except Exception:
                                # Corrupt complete line; skip and continue.
                                inbox_offset_bytes = inbox_file.tell()
                                continue

                            if event.get("type") == "user":
                                injection_id = event.get("injection_id")
                                last_injection_id = injection_id
                                content = event.get("content", "")
                                if fresh_thread_per_injection:
                                    if persistent_preamble is None:
                                        persistent_preamble = content
                                        write_event(outbox, {
                                            "type": "worker_trace",
                                            "job_id": job_id,
                                            "node_id": node_id,
                                            "injection_id": injection_id,
                                            "event": "persistent_preamble_captured",
                                        })
                                        write_event(outbox, {
                                            "type": "complete",
                                            "job_id": job_id,
                                            "node_id": node_id,
                                            "injection_id": injection_id,
                                        })
                                    else:
                                        effective_content = content
                                        if isinstance(persistent_preamble, str) and persistent_preamble.strip():
                                            effective_content = f"{persistent_preamble.rstrip()}\n\n{content}"
                                        _start_fresh_turn(injection_id, effective_content)
                                        write_event(outbox, {
                                            "type": "worker_trace",
                                            "job_id": job_id,
                                            "node_id": node_id,
                                            "injection_id": injection_id,
                                            "event": "fresh_thread_requested",
                                        })
                                elif current_active_turn_id:
                                    _send_user_turn_steer(injection_id, content)
                                    write_event(outbox, {
                                        "type": "worker_trace",
                                        "job_id": job_id,
                                        "node_id": node_id,
                                        "injection_id": injection_id,
                                        "event": "turn_steer_sent",
                                        "turn_id": current_active_turn_id,
                                    })
                                else:
                                    _send_user_turn_start(injection_id, content)
                                    write_event(outbox, {
                                        "type": "worker_trace",
                                        "job_id": job_id,
                                        "node_id": node_id,
                                        "injection_id": injection_id,
                                        "event": "turn_start_sent",
                                    })

                            elif event.get("type") == "control":
                                payload = event.get("payload", {})

                                if payload.get("type") == "rpc_response":
                                    response_id = payload.get("rpc_id")
                                    result = payload.get("result")
                                    if response_id in pending_dynamic_tool_requests:
                                        decision = result.get("decision") if isinstance(result, dict) else None
                                        approved = decision in (
                                            "accept",
                                            "approved",
                                            "acceptForSession",
                                            "approved_for_session",
                                        )
                                        _handle_dynamic_exec_approval(response_id, approved)
                                        inbox_offset_bytes = inbox_file.tell()
                                        continue
                                    # Preserve local request id counter type; inbox rpc ids can be
                                    # int or string depending on upstream request shape.
                                    send_response(response_id, result)
                                else:
                                    method = payload.get("method")
                                    params = payload.get("params")

                                    if method:
                                        if method == "exec/approvalResponse" and isinstance(params, dict):
                                            call_id = params.get("call_id") or params.get("callId")
                                            if isinstance(call_id, str) and call_id in pending_dynamic_tool_calls:
                                                request_id = pending_dynamic_tool_calls.get(call_id)
                                                approved = bool(params.get("approved")) or params.get("decision") in (
                                                    "accept",
                                                    "approved",
                                                    "acceptForSession",
                                                    "approved_for_session",
                                                )
                                                if isinstance(request_id, int):
                                                    _handle_dynamic_exec_approval(request_id, approved)
                                                inbox_offset_bytes = inbox_file.tell()
                                                continue
                                            if isinstance(call_id, str) and call_id in pending_session_tool_calls:
                                                approved = bool(params.get("approved")) or params.get("decision") in (
                                                    "accept",
                                                    "approved",
                                                    "acceptForSession",
                                                    "approved_for_session",
                                                )
                                                info = pending_session_tool_calls.get(call_id)
                                                if isinstance(info, dict):
                                                    info["approved"] = approved
                                                    info["decision_received_ts"] = time.time()
                                                    info["defer_until_ts"] = time.time() + SESSION_TOOL_NATIVE_GRACE_SECONDS
                                                    info["state"] = "deferred"
                                                inbox_offset_bytes = inbox_file.tell()
                                                continue
                                        # Forward control notification directly to Codex app-server
                                        send_notification(method, params)

                            elif event.get("type") == "shutdown":
                                running = False
                                inbox_offset_bytes = inbox_file.tell()
                                break
                            inbox_offset_bytes = inbox_file.tell()
                except Exception as e:
                    write_event(outbox, {
                        "type": "worker_error",
                        "error": f"inbox_tail_error: {str(e)}"
                    })

            # --- Session file tailing for internal function_call artifacts ---
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

                        if capture_all_session or session_trace_lines_remaining > 0:
                            write_event(outbox, {
                                "type": "session_trace",
                                "job_id": job_id,
                                "node_id": node_id,
                                "injection_id": last_injection_id,
                                "reason": session_trace_reason or ("full_capture" if capture_all_session else None),
                                "turn_id": session_trace_turn_id,
                                "entry": entry,
                            })
                            if session_trace_lines_remaining > 0:
                                session_trace_lines_remaining -= 1

                        if entry.get("type") != "response_item":
                            continue
                        payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
                        if payload:
                            session_response_history.append(payload)

                        if payload.get("type") == "function_call_output":
                            _finish_session_tool_call(payload.get("call_id"))
                            continue

                        if payload.get("type") == "custom_tool_call_output":
                            _finish_session_tool_call(payload.get("call_id"))
                            continue

                        if (
                            payload.get("type") == "function_call"
                            and payload.get("name") == "exec_command"
                            and isinstance(payload.get("call_id"), str)
                        ):
                            call_id = payload.get("call_id")
                            try:
                                arguments = json.loads(payload.get("arguments") or "{}")
                            except Exception:
                                arguments = {}
                            if (
                                isinstance(arguments, dict)
                                and arguments.get("sandbox_permissions") == "require_escalated"
                                and call_id not in pending_session_tool_calls
                                and call_id not in pending_dynamic_tool_calls
                            ):
                                pending_session_tool_calls[call_id] = {
                                    "tool_type": "function_call_output",
                                    "arguments": arguments,
                                    "injection_id": last_injection_id,
                                    "state": "pending",
                                    "native_seen": False,
                                }
                            continue

                        if (
                            payload.get("type") == "custom_tool_call"
                            and payload.get("name") == "apply_patch"
                            and isinstance(payload.get("call_id"), str)
                        ):
                            call_id = payload.get("call_id")
                            if call_id not in pending_session_tool_calls:
                                pending_session_tool_calls[call_id] = {
                                    "tool_type": "custom_tool_call_output",
                                    "input": payload.get("input") or "",
                                    "injection_id": last_injection_id,
                                    "state": "pending",
                                    "native_seen": False,
                                }
                except Exception as e:
                    write_event(outbox, {
                        "type": "worker_error",
                        "error": f"session_tail_error: {str(e)}"
                    })

            for deferred_call_id, info in list(pending_session_tool_calls.items()):
                if not isinstance(info, dict):
                    continue
                if info.get("state") != "deferred":
                    continue
                if info.get("native_seen"):
                    info["state"] = "native"
                    continue
                defer_until_ts = info.get("defer_until_ts")
                if not isinstance(defer_until_ts, (int, float)):
                    continue
                if time.time() < float(defer_until_ts):
                    continue
                _fulfill_session_tool_call(deferred_call_id, bool(info.get("approved")))

            if proc.poll() is not None:
                if shutdown_requested:
                    running = False
                else:
                    if not restart_codex("proc_exited", outbox):
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
