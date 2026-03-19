#!/usr/bin/env python3
"""
Probe Codex approval semantics through the production worker/mailbox path.

This harness runs `agent/codex_worker.py` in isolation (single node), injects a
prompt through inbox JSONL, captures approval requests from outbox JSONL, and
tests approval response variants by writing control messages back to inbox.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


APPROVAL_METHODS = {
    "codex/event/exec_approval_request",
    "item/commandExecution/requestApproval",
    "codex/event/apply_patch_approval_request",
    "item/fileChange/requestApproval",
    "execCommandApproval",
    "applyPatchApproval",
}


DEFAULT_PROMPT = (
    "Randomly select a 1980s video game and implement a version that can run in a web browser. "
    "Use shell commands as needed to create files."
)

FILECHANGE_PROMPT = (
    "Use apply_patch to create a new file named probe_patch.txt containing exactly one line: PROBE_PATCH_OK"
)

FILECHANGE_STRICT_PROMPT = (
    "Do not use exec_command. Use apply_patch exactly once to add a new file named "
    "probe_patch_strict.txt containing exactly one line: PROBE_PATCH_STRICT_OK. "
    "Then report whether patch approval was granted."
)

IMPLICIT_APPLYPATCH_PROMPT = (
    "Do not use exec_command. Use apply_patch for every file edit. Create exactly one file "
    "named index.html. In the first apply_patch, create index.html as a tiny browser game and "
    "intentionally include two defects: (1) an external Google Fonts stylesheet link, and "
    "(2) a visible bug where the on-page title says BROKEN BUILD. Immediately after the first "
    "patch, inspect your own work and use a second apply_patch to remove the external font "
    "dependency and change BROKEN BUILD to FINAL BUILD. Do not stop after the first patch. "
    "Your final answer should say whether the second apply_patch succeeded."
)

OUTSIDE_WRITE_PROMPT = (
    "Use exec_command exactly once with sandbox_permissions=require_escalated to run: "
    "/bin/bash -lc 'echo PROBE_OUTSIDE_WRITE >/usr/local/share/codeswarm_probe_worker_outside.txt'. "
    "Then report whether approval was granted."
)

NETWORK_PROMPT = (
    "Use exec_command exactly once with sandbox_permissions=require_escalated to run: "
    "/bin/bash -lc 'curl -I https://example.com >/tmp/codeswarm_probe_network.txt'. "
    "Then report whether approval was granted."
)

SCENARIO_PROMPTS = {
    "command": DEFAULT_PROMPT,
    "filechange": FILECHANGE_PROMPT,
    "filechange_strict": FILECHANGE_STRICT_PROMPT,
    "implicit_apply_patch": IMPLICIT_APPLYPATCH_PROMPT,
    "outside_write": OUTSIDE_WRITE_PROMPT,
    "network": NETWORK_PROMPT,
}


@dataclass
class ProbeResult:
    variant: str
    approval_seen: bool
    approval_method: str | None
    call_id: str | None
    request_id: Any
    command_started: bool
    command_completed: bool
    filechange_started: bool
    filechange_completed: bool
    session_apply_patch_seen: bool
    session_apply_patch_output_seen: bool
    task_complete: bool
    error: str | None
    run_dir: str | None


def _approval_from_payload(payload: dict[str, Any]) -> tuple[str | None, Any, str | None]:
    method = payload.get("method")
    if method not in APPROVAL_METHODS:
        return (None, None, None)
    params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
    call_id: str | None = None
    request_id = params.get("id")
    if method in ("codex/event/exec_approval_request", "codex/event/apply_patch_approval_request"):
        msg = params.get("msg") if isinstance(params.get("msg"), dict) else {}
        call_id = msg.get("call_id")
    elif method in ("execCommandApproval", "applyPatchApproval"):
        call_id = params.get("callId")
        request_id = payload.get("id")
    else:
        call_id = params.get("itemId")
        if request_id is None:
            request_id = payload.get("id")
    return (method, request_id, call_id)


def _variant_payloads(
    variant: str,
    request_id: Any,
    item_request_id: Any,
    call_id: str | None,
    proposed_execpolicy_amendment: list[Any] | None,
) -> list[dict[str, Any]]:
    if variant == "rpc_approved":
        if request_id is None:
            return []
        return [{"type": "rpc_response", "rpc_id": request_id, "result": {"decision": "approved", "approved": True}}]
    if variant == "rpc_accept":
        if request_id is None:
            return []
        return [{"type": "rpc_response", "rpc_id": request_id, "result": {"decision": "accept", "approved": True}}]
    if variant == "notify_accept":
        return [{"method": "exec/approvalResponse", "params": {"call_id": call_id, "callId": call_id, "decision": "accept", "approved": True}}]
    if variant == "notify_approved":
        return [{"method": "exec/approvalResponse", "params": {"call_id": call_id, "callId": call_id, "decision": "approved", "approved": True}}]
    if variant == "notify_accept_bare":
        return [{"method": "exec/approvalResponse", "params": {"call_id": call_id, "callId": call_id, "decision": "accept"}}]
    if variant == "notify_approved_bare":
        return [{"method": "exec/approvalResponse", "params": {"call_id": call_id, "callId": call_id, "decision": "approved"}}]
    if variant == "notify_accept_for_session_bare":
        return [{"method": "exec/approvalResponse", "params": {"call_id": call_id, "callId": call_id, "decision": "acceptForSession"}}]
    if variant == "rpc_plus_notify":
        payloads: list[dict[str, Any]] = []
        if request_id is not None:
            payloads.append({"type": "rpc_response", "rpc_id": request_id, "result": {"decision": "approved", "approved": True}})
        payloads.append({"method": "exec/approvalResponse", "params": {"call_id": call_id, "callId": call_id, "decision": "accept", "approved": True}})
        return payloads
    if variant == "rpc_item_approved":
        if item_request_id is None:
            return []
        return [{"type": "rpc_response", "rpc_id": item_request_id, "result": {"decision": "approved", "approved": True}}]
    if variant == "rpc_item_accept":
        if item_request_id is None:
            return []
        return [{"type": "rpc_response", "rpc_id": item_request_id, "result": {"decision": "accept", "approved": True}}]
    if variant == "rpc_item_plus_notify":
        payloads: list[dict[str, Any]] = []
        if item_request_id is not None:
            payloads.append({"type": "rpc_response", "rpc_id": item_request_id, "result": {"decision": "accept", "approved": True}})
        payloads.append({"method": "exec/approvalResponse", "params": {"call_id": call_id, "callId": call_id, "decision": "accept", "approved": True}})
        return payloads
    if variant == "rpc_approved_execpolicy":
        if request_id is None:
            return []
        amendment = proposed_execpolicy_amendment if isinstance(proposed_execpolicy_amendment, list) else []
        return [
            {
                "type": "rpc_response",
                "rpc_id": request_id,
                "result": {
                    "decision": {
                        "approved_execpolicy_amendment": {
                            "proposed_execpolicy_amendment": amendment
                        }
                    },
                    "approved": True,
                },
            }
        ]
    if variant == "notify_accept_execpolicy":
        amendment = proposed_execpolicy_amendment if isinstance(proposed_execpolicy_amendment, list) else []
        decision_obj = {"acceptWithExecpolicyAmendment": {"execpolicy_amendment": amendment}}
        return [
            {
                "method": "exec/approvalResponse",
                "params": {
                    "call_id": call_id,
                    "callId": call_id,
                    "decision": decision_obj,
                    "approved": True,
                    **decision_obj,
                },
            }
        ]
    if variant == "rpc_execpolicy_plus_notify":
        amendment = proposed_execpolicy_amendment if isinstance(proposed_execpolicy_amendment, list) else []
        decision_obj = {"acceptWithExecpolicyAmendment": {"execpolicy_amendment": amendment}}
        payloads = []
        if request_id is not None:
            payloads.append(
                {
                    "type": "rpc_response",
                    "rpc_id": request_id,
                    "result": {
                        "decision": {
                            "approved_execpolicy_amendment": {
                                "proposed_execpolicy_amendment": amendment
                            }
                        },
                        "approved": True,
                    },
                }
            )
        payloads.append(
            {
                "method": "exec/approvalResponse",
                "params": {
                    "call_id": call_id,
                    "callId": call_id,
                    "decision": decision_obj,
                    "approved": True,
                    **decision_obj,
                },
            }
        )
        return payloads
    if variant == "rpc_abort":
        if request_id is None:
            return []
        return [{"type": "rpc_response", "rpc_id": request_id, "result": {"decision": "abort", "approved": False}}]
    if variant == "rpc_cancel":
        if request_id is None:
            return []
        return [{"type": "rpc_response", "rpc_id": request_id, "result": {"decision": "cancel", "approved": False}}]
    if variant == "notify_abort":
        return [
            {
                "method": "exec/approvalResponse",
                "params": {"call_id": call_id, "callId": call_id, "decision": "abort", "approved": False},
            }
        ]
    if variant == "notify_cancel":
        return [
            {
                "method": "exec/approvalResponse",
                "params": {"call_id": call_id, "callId": call_id, "decision": "cancel", "approved": False},
            }
        ]
    if variant == "notify_approved_with_id":
        return [
            {
                "method": "exec/approvalResponse",
                "params": {
                    "id": request_id,
                    "turn_id": request_id,
                    "turnId": request_id,
                    "call_id": call_id,
                    "callId": call_id,
                    "decision": "approved",
                    "approved": True,
                },
            }
        ]
    if variant == "notify_accept_with_id":
        return [
            {
                "method": "exec/approvalResponse",
                "params": {
                    "id": request_id,
                    "turn_id": request_id,
                    "turnId": request_id,
                    "call_id": call_id,
                    "callId": call_id,
                    "decision": "accept",
                    "approved": True,
                },
            }
        ]
    raise ValueError(f"unknown variant: {variant}")


def _append_jsonl(path: Path, obj: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj) + "\n")


def _tail_jsonl(path: Path, offset: int) -> tuple[list[dict[str, Any]], int]:
    if not path.exists():
        return [], offset
    size = path.stat().st_size
    if offset > size:
        offset = 0
    with path.open("rb") as f:
        f.seek(offset)
        rows: list[dict[str, Any]] = []
        while True:
            line_start = f.tell()
            raw = f.readline()
            if not raw:
                break
            if not raw.endswith(b"\n"):
                f.seek(line_start)
                break
            try:
                obj = json.loads(raw.decode("utf-8"))
            except Exception:
                continue
            rows.append(obj)
        return rows, f.tell()


def run_variant(
    *,
    worker_py: Path,
    codex_bin: str,
    prompt: str,
    variant: str,
    pre_timeout: float,
    post_timeout: float,
    keep_dir: str | None,
    native_wait_s: float,
    scenario: str,
    ask_for_approval: str,
    sandbox_mode: str,
) -> ProbeResult:
    probe_root = worker_py.parent.parent / ".tmp" / "approval-probes"
    probe_root.mkdir(parents=True, exist_ok=True)
    tmp_obj = tempfile.TemporaryDirectory(prefix="codeswarm-approval-probe-", dir=str(probe_root))
    tmp = tmp_obj.name
    base = Path(tmp)
    preserved_dir: str | None = None
    try:
        inbox = base / "mailbox" / "inbox" / "probejob_00.jsonl"
        outbox = base / "mailbox" / "outbox" / "probejob_00.jsonl"
        env = os.environ.copy()
        env["CODESWARM_JOB_ID"] = "probejob"
        env["CODESWARM_NODE_ID"] = "0"
        env["CODESWARM_BASE_DIR"] = str(base)
        env["CODESWARM_CODEX_BIN"] = codex_bin
        env["CODESWARM_ASK_FOR_APPROVAL"] = ask_for_approval
        env["CODESWARM_SANDBOX_MODE"] = sandbox_mode

        proc = subprocess.Popen(
            [sys.executable, str(worker_py)],
            cwd=str(base),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )

        offset = 0
        approval_seen = False
        approval_method: str | None = None
        request_id: Any = None
        item_request_id: Any = None
        call_id: str | None = None
        proposed_execpolicy_amendment: list[Any] | None = None
        command_started = False
        command_completed = False
        filechange_started = False
        filechange_completed = False
        session_apply_patch_seen = False
        session_apply_patch_output_seen = False
        task_complete = False
        error: str | None = None

        start_seen = False
        codex_rpc_count = 0
        worker_errors: list[str] = []
        rpc_errors: list[dict[str, Any]] = []
        stream_errors: list[dict[str, Any]] = []
        last_methods: list[str] = []

        try:
            # Wait for worker start marker.
            start_deadline = time.time() + 12.0
            while time.time() < start_deadline:
                rows, offset = _tail_jsonl(outbox, offset)
                for r in rows:
                    if r.get("type") == "start":
                        start_seen = True
                    elif r.get("type") == "codex_rpc":
                        codex_rpc_count += 1
                        payload = r.get("payload")
                        if isinstance(payload, dict):
                            m = payload.get("method")
                            if isinstance(m, str):
                                last_methods.append(m)
                                if len(last_methods) > 12:
                                    last_methods = last_methods[-12:]
                    elif r.get("type") == "worker_error":
                        worker_errors.append(str(r.get("error")))
                        if len(worker_errors) > 5:
                            worker_errors = worker_errors[-5:]
                if start_seen:
                    break
                if proc.poll() is not None:
                    raise RuntimeError(f"worker exited early: {proc.returncode}")
                time.sleep(0.1)
            if not start_seen:
                raise RuntimeError("worker did not emit start event")

            injection_id = str(uuid.uuid4())
            _append_jsonl(inbox, {"type": "user", "injection_id": injection_id, "content": prompt})

            deadline = time.time() + pre_timeout
            while time.time() < deadline:
                rows, offset = _tail_jsonl(outbox, offset)
                for row in rows:
                    row_type = row.get("type")
                    if row_type == "session_trace":
                        entry = row.get("entry") if isinstance(row.get("entry"), dict) else {}
                        if entry.get("type") == "response_item":
                            sp = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
                            if (
                                sp.get("type") == "custom_tool_call"
                                and sp.get("name") == "apply_patch"
                                and isinstance(sp.get("call_id"), str)
                            ):
                                session_apply_patch_seen = True
                                if not approval_seen:
                                    approval_seen = True
                                    approval_method = "session/custom_tool_call/apply_patch"
                                    request_id = None
                                    call_id = sp.get("call_id")
                        continue
                    if row_type != "codex_rpc":
                        if row_type == "worker_error":
                            worker_errors.append(str(row.get("error")))
                            if len(worker_errors) > 5:
                                worker_errors = worker_errors[-5:]
                        continue
                    codex_rpc_count += 1
                    payload = row.get("payload")
                    if not isinstance(payload, dict):
                        continue
                    m = payload.get("method")
                    if isinstance(m, str):
                        last_methods.append(m)
                        if len(last_methods) > 12:
                            last_methods = last_methods[-12:]
                        if m == "codex/event/stream_error":
                            stream_errors.append(payload)
                            if len(stream_errors) > 3:
                                stream_errors = stream_errors[-3:]
                    if payload.get("error") is not None:
                        rpc_errors.append(payload)
                        if len(rpc_errors) > 3:
                            rpc_errors = rpc_errors[-3:]
                    method, rid, cid = _approval_from_payload(payload)
                    if (
                        isinstance(payload.get("id"), int)
                        and method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval")
                    ):
                        if (call_id is None) or (cid == call_id):
                            item_request_id = payload.get("id")
                    if method:
                        params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
                        msg = params.get("msg") if isinstance(params.get("msg"), dict) else {}
                        amendment = msg.get("proposed_execpolicy_amendment")
                        if not approval_seen:
                            approval_seen = True
                            approval_method = method
                            request_id = rid
                            call_id = cid
                            if isinstance(amendment, list):
                                proposed_execpolicy_amendment = amendment
                        else:
                            # Keep strongest shape for same call_id.
                            if call_id and cid == call_id and rid is not None and request_id is None:
                                request_id = rid
                                approval_method = method
                            if call_id and cid == call_id and isinstance(payload.get("id"), int):
                                item_request_id = payload.get("id")
                            if call_id and cid == call_id and isinstance(amendment, list):
                                proposed_execpolicy_amendment = amendment
                if approval_seen and (request_id is not None or approval_method == "session/custom_tool_call/apply_patch"):
                    break
                if proc.poll() is not None:
                    break
                time.sleep(0.1)

            if approval_seen:
                # If first observed shape has no request-id, allow a short window for
                # native codex/event/* approval for the same call_id to arrive.
                if request_id is None and call_id:
                    settle_deadline = time.time() + max(0.5, native_wait_s)
                    while time.time() < settle_deadline and request_id is None:
                        rows, offset = _tail_jsonl(outbox, offset)
                        for row in rows:
                            if row.get("type") != "codex_rpc":
                                continue
                            payload = row.get("payload")
                            if not isinstance(payload, dict):
                                continue
                            method, rid, cid = _approval_from_payload(payload)
                            if method and cid == call_id and rid is not None:
                                request_id = rid
                                approval_method = method
                                break
                        if request_id is not None:
                            break
                        time.sleep(0.05)

                for payload in _variant_payloads(
                    variant,
                    request_id,
                    item_request_id,
                    call_id,
                    proposed_execpolicy_amendment,
                ):
                    _append_jsonl(inbox, {"type": "control", "payload": payload})

            deadline = time.time() + post_timeout
            while time.time() < deadline:
                rows, offset = _tail_jsonl(outbox, offset)
                for row in rows:
                    if row.get("type") != "codex_rpc":
                        if row.get("type") == "worker_error":
                            worker_errors.append(str(row.get("error")))
                            if len(worker_errors) > 5:
                                worker_errors = worker_errors[-5:]
                        continue
                    codex_rpc_count += 1
                    payload = row.get("payload")
                    if not isinstance(payload, dict):
                        continue
                    method = payload.get("method")
                    if isinstance(method, str):
                        last_methods.append(method)
                        if len(last_methods) > 12:
                            last_methods = last_methods[-12:]
                        if method == "codex/event/stream_error":
                            stream_errors.append(payload)
                            if len(stream_errors) > 3:
                                stream_errors = stream_errors[-3:]
                    if payload.get("error") is not None:
                        rpc_errors.append(payload)
                        if len(rpc_errors) > 3:
                            rpc_errors = rpc_errors[-3:]
                    params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
                    msg = params.get("msg") if isinstance(params.get("msg"), dict) else {}

                    if method == "codex/event/exec_command_begin":
                        if call_id is None or msg.get("call_id") == call_id:
                            command_started = True
                    elif method == "codex/event/exec_command_end":
                        if call_id is None or msg.get("call_id") == call_id:
                            command_completed = True
                    elif method == "codex/event/patch_apply_begin":
                        if call_id is None or msg.get("call_id") == call_id:
                            filechange_started = True
                    elif method == "codex/event/patch_apply_end":
                        if call_id is None or msg.get("call_id") == call_id:
                            filechange_completed = True
                    elif method in ("item/started", "item/completed"):
                        item = params.get("item") if isinstance(params.get("item"), dict) else {}
                        item_type = str(item.get("type") or "").lower()
                        item_id = item.get("id")
                        if item_type == "filechange" and (call_id is None or item_id == call_id):
                            if method == "item/started":
                                filechange_started = True
                            else:
                                filechange_completed = True
                    elif method in ("codex/event/task_complete", "task/complete"):
                        task_complete = True
                for row in rows:
                    if row.get("type") == "session_trace":
                        entry = row.get("entry") if isinstance(row.get("entry"), dict) else {}
                        if entry.get("type") == "response_item":
                            sp = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
                            if (
                                sp.get("type") == "custom_tool_call"
                                and sp.get("name") == "apply_patch"
                                and (call_id is None or sp.get("call_id") == call_id)
                            ):
                                session_apply_patch_seen = True
                            elif (
                                sp.get("type") == "custom_tool_call_output"
                                and (call_id is None or sp.get("call_id") == call_id)
                            ):
                                session_apply_patch_output_seen = True

                command_like = scenario in ("command", "outside_write", "network")
                completed = command_completed if command_like else (
                    filechange_completed or session_apply_patch_output_seen
                )
                if completed:
                    break
                if proc.poll() is not None:
                    break
                time.sleep(0.1)

        except Exception as exc:
            error = str(exc)
        finally:
            try:
                _append_jsonl(inbox, {"type": "shutdown"})
            except Exception:
                pass
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                proc.wait(timeout=4)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

        if keep_dir:
            target = Path(keep_dir).expanduser().resolve()
            target.mkdir(parents=True, exist_ok=True)
            dest = target / base.name
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(base, dest)
            preserved_dir = str(dest)

        return ProbeResult(
            variant=variant,
            approval_seen=approval_seen,
            approval_method=approval_method,
            call_id=call_id,
            request_id=request_id,
            command_started=command_started,
            command_completed=command_completed,
            filechange_started=filechange_started,
            filechange_completed=filechange_completed,
            session_apply_patch_seen=session_apply_patch_seen,
            session_apply_patch_output_seen=session_apply_patch_output_seen,
            task_complete=task_complete,
            error=(
                error
                or (
                    f"no_approval_observed start_seen={start_seen} "
                    f"codex_rpc_count={codex_rpc_count} "
                    f"last_methods={last_methods} worker_errors={worker_errors}"
                    f" stream_errors={stream_errors} rpc_errors={rpc_errors}"
                    if not approval_seen
                    else None
                )
            ),
            run_dir=preserved_dir,
        )
    finally:
        try:
            tmp_obj.cleanup()
        except Exception:
            pass


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Probe approval semantics through codex_worker mailbox path.")
    p.add_argument("--worker", default="agent/codex_worker.py")
    p.add_argument("--codex-bin", default=os.environ.get("CODESWARM_CODEX_BIN", "codex"))
    p.add_argument("--prompt", default=DEFAULT_PROMPT)
    p.add_argument(
        "--scenario",
        choices=["command", "filechange", "filechange_strict", "implicit_apply_patch", "outside_write", "network"],
        default="command",
        help="Type of approval flow to trigger.",
    )
    p.add_argument(
        "--scenarios",
        default="",
        help="Comma-separated scenario sweep. If set, runs all listed scenarios in order.",
    )
    p.add_argument(
        "--ask-for-approval",
        default="untrusted",
        help="Approval policy passed to codex_worker (e.g. untrusted, on-request, never).",
    )
    p.add_argument(
        "--sandbox",
        default="workspace-write",
        help="Sandbox mode passed to codex_worker.",
    )
    p.add_argument(
        "--variants",
        default="rpc_approved,rpc_accept,notify_accept,notify_approved,notify_accept_bare,notify_approved_bare,notify_accept_for_session_bare,rpc_plus_notify,rpc_approved_execpolicy,notify_accept_execpolicy,rpc_execpolicy_plus_notify,rpc_abort,rpc_cancel,notify_abort,notify_cancel",
        help="Comma-separated response variants.",
    )
    p.add_argument("--pre-timeout", type=float, default=70.0)
    p.add_argument("--post-timeout", type=float, default=30.0)
    p.add_argument(
        "--native-wait",
        type=float,
        default=20.0,
        help="Seconds to wait for native codex/event approval for same call_id after synthetic item/* approval.",
    )
    p.add_argument(
        "--keep-dir",
        default="",
        help="If set, copy probe mailbox artifacts to this directory for debugging.",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    variants = [v.strip() for v in args.variants.split(",") if v.strip()]
    worker_py = Path(args.worker).resolve()
    scenarios = [s.strip() for s in args.scenarios.split(",") if s.strip()] if args.scenarios else [args.scenario]

    print("# approval-worker-probe start", flush=True)
    print(
        json.dumps(
            {
                "worker": str(worker_py),
                "codex_bin": args.codex_bin,
                "variants": variants,
                "scenarios": scenarios,
            }
        ),
        flush=True,
    )

    results: list[ProbeResult] = []
    for scenario in scenarios:
        prompt = args.prompt
        if prompt == DEFAULT_PROMPT:
            prompt = SCENARIO_PROMPTS.get(scenario, DEFAULT_PROMPT)
        print(f"# scenario {scenario}", flush=True)
        for variant in variants:
            print(f"# variant {variant}", flush=True)
            result = run_variant(
                worker_py=worker_py,
                codex_bin=args.codex_bin,
                prompt=prompt,
                variant=variant,
                pre_timeout=args.pre_timeout,
                post_timeout=args.post_timeout,
                keep_dir=(args.keep_dir or None),
                native_wait_s=args.native_wait,
                scenario=scenario,
                ask_for_approval=args.ask_for_approval,
                sandbox_mode=args.sandbox,
            )
            result_dict = dict(result.__dict__)
            result_dict["scenario"] = scenario
            results.append(result)
            print(json.dumps(result_dict), flush=True)

    summary = {
        "approval_seen_variants": [r.variant for r in results if r.approval_seen],
        "started_variants": [r.variant for r in results if r.command_started],
        "completed_variants": [
            r.variant for r in results
            if (r.command_completed or r.filechange_completed or r.session_apply_patch_output_seen)
        ],
        "errors": [r.__dict__ for r in results if r.error],
    }
    print("# approval-worker-probe summary", flush=True)
    print(json.dumps(summary), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
