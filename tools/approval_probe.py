#!/usr/bin/env python3
"""
Deterministic approval-protocol probe for Codex app-server.

This script launches `codex app-server --listen stdio://`, triggers an approval
request, and tests response shapes to determine which payloads unblock command
execution for a given runtime.
"""

from __future__ import annotations

import argparse
import json
import os
import select
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any


APPROVAL_METHODS = {
    "codex/event/exec_approval_request",
    "item/commandExecution/requestApproval",
    "codex/event/apply_patch_approval_request",
    "item/fileChange/requestApproval",
}


DEFAULT_PROMPT = (
    "Run exactly one command using exec_command with sandbox_permissions=require_escalated: "
    "/bin/bash -lc \"echo APPROVAL_PROBE_OK > approval_probe.txt && ls -l approval_probe.txt\". "
    "Then stop and report the command output."
)

FILECHANGE_PROMPT = (
    "Use apply_patch to create a new file named probe_patch.txt containing exactly one line: PROBE_PATCH_OK"
)

FILECHANGE_STRICT_PROMPT = (
    "Do not use exec_command. Use apply_patch exactly once to add a new file named "
    "probe_patch_strict.txt containing exactly one line: PROBE_PATCH_STRICT_OK. "
    "Then report whether patch approval was granted."
)

FILECHANGE_PROMPT = (
    "Use apply_patch to create a new file named probe_patch.txt containing exactly one line: PROBE_PATCH_OK"
)

OUTSIDE_WRITE_PROMPT = (
    "Use exec_command exactly once with sandbox_permissions=require_escalated to run: "
    "/bin/bash -lc 'echo PROBE_OUTSIDE_WRITE >/usr/local/share/codeswarm_probe_outside.txt'. "
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
    "outside_write": OUTSIDE_WRITE_PROMPT,
    "network": NETWORK_PROMPT,
}

FORCE_ESCALATION_PREFIX = (
    "Before anything else, you MUST call exec_command with sandbox_permissions=require_escalated "
    "at least once, and wait for approval resolution before continuing."
)


@dataclass
class ProbeResult:
    variant: str
    approval_seen: bool
    approval_method: str | None
    call_id: str | None
    request_id: Any
    command_started: bool
    command_completed: bool
    task_complete: bool
    error: str | None


class AppServerSession:
    def __init__(self, codex_bin: str, sandbox: str, ask_for_approval: str) -> None:
        self.proc = subprocess.Popen(
            [
                codex_bin,
                "--sandbox",
                sandbox,
                "--ask-for-approval",
                ask_for_approval,
                "app-server",
                "--listen",
                "stdio://",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._rpc_id = 0
        self._thread_id: str | None = None

    def close(self) -> None:
        try:
            self.proc.terminate()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=2)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass

    def _next_id(self) -> int:
        rid = self._rpc_id
        self._rpc_id += 1
        return rid

    def send(self, payload: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()

    def send_request(self, method: str, params: dict[str, Any] | None = None) -> int:
        rid = self._next_id()
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            payload["params"] = params
        self.send(payload)
        return rid

    def send_notification(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self.send(payload)

    def read_messages(self, timeout_s: float) -> list[dict[str, Any]]:
        assert self.proc.stdout is not None
        deadline = time.time() + timeout_s
        out: list[dict[str, Any]] = []
        while time.time() < deadline:
            remain = max(0.0, deadline - time.time())
            ready, _, _ = select.select([self.proc.stdout], [], [], min(0.2, remain))
            if not ready:
                continue
            line = self.proc.stdout.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
        return out

    def initialize(self, timeout_s: float = 8.0) -> None:
        init_id = self.send_request(
            "initialize",
            {
                "processId": None,
                "rootUri": None,
                "capabilities": {},
                "clientInfo": {"name": "approval-probe", "version": "0.1.0"},
            },
        )

        got_init = False
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            for msg in self.read_messages(0.5):
                if msg.get("id") == init_id and isinstance(msg.get("result"), dict):
                    got_init = True
            if got_init:
                break
        if not got_init:
            raise RuntimeError("initialize failed or timed out")

        self.send_notification("initialized", {})
        thread_req_id = self.send_request("thread/start", {})
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            for msg in self.read_messages(0.5):
                if msg.get("id") == thread_req_id and isinstance(msg.get("result"), dict):
                    thread = msg["result"].get("thread")
                    if isinstance(thread, dict) and isinstance(thread.get("id"), str):
                        self._thread_id = thread["id"]
                        return
                if (
                    msg.get("method") == "thread/status/changed"
                    and isinstance(msg.get("params"), dict)
                    and isinstance(msg["params"].get("threadId"), str)
                ):
                    self._thread_id = msg["params"]["threadId"]
                    return
        raise RuntimeError("thread/start failed or timed out")

    def start_turn(self, prompt: str) -> int:
        if not self._thread_id:
            raise RuntimeError("thread not initialized")
        return self.send_request(
            "turn/start",
            {
                "threadId": self._thread_id,
                "input": [{"type": "text", "text": prompt}],
            },
        )


def _approval_from_msg(msg: dict[str, Any]) -> tuple[str | None, Any, str | None]:
    method = msg.get("method")
    if method not in APPROVAL_METHODS:
        return (None, None, None)
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
    call_id: str | None = None
    request_id = None
    request_id = params.get("id")
    if method in ("codex/event/exec_approval_request", "codex/event/apply_patch_approval_request"):
        payload_msg = params.get("msg") if isinstance(params.get("msg"), dict) else {}
        call_id = payload_msg.get("call_id")
        if request_id is None:
            request_id = msg.get("id")
    else:
        call_id = params.get("itemId")
        if request_id is None:
            request_id = msg.get("id")
    return (method, request_id, call_id)


def _build_response_payloads(variant: str, request_id: Any, call_id: str | None) -> list[dict[str, Any]]:
    if variant == "rpc_approved":
        return [
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"decision": "approved", "approved": True},
            }
        ]
    if variant == "rpc_accept":
        return [
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"decision": "accept", "approved": True},
            }
        ]
    if variant == "notify_accept":
        return [
            {
                "jsonrpc": "2.0",
                "method": "exec/approvalResponse",
                "params": {"call_id": call_id, "callId": call_id, "decision": "accept", "approved": True},
            }
        ]
    if variant == "notify_approved":
        return [
            {
                "jsonrpc": "2.0",
                "method": "exec/approvalResponse",
                "params": {"call_id": call_id, "callId": call_id, "decision": "approved", "approved": True},
            }
        ]
    if variant == "rpc_plus_notify":
        return [
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"decision": "approved", "approved": True},
            },
            {
                "jsonrpc": "2.0",
                "method": "exec/approvalResponse",
                "params": {"call_id": call_id, "callId": call_id, "decision": "accept", "approved": True},
            },
        ]
    raise ValueError(f"unknown variant: {variant}")


def run_probe_variant(
    *,
    codex_bin: str,
    sandbox: str,
    ask_for_approval: str,
    prompt: str,
    variant: str,
    pre_approval_timeout_s: float,
    post_approval_timeout_s: float,
) -> ProbeResult:
    session = AppServerSession(codex_bin=codex_bin, sandbox=sandbox, ask_for_approval=ask_for_approval)
    approval_method: str | None = None
    request_id = None
    call_id: str | None = None
    command_started = False
    command_completed = False
    task_complete = False
    error: str | None = None
    approval_seen = False
    try:
        session.initialize()
        session.start_turn(prompt)

        # Wait for first approval.
        deadline = time.time() + pre_approval_timeout_s
        while time.time() < deadline and not approval_seen:
            for msg in session.read_messages(0.4):
                method, rid, cid = _approval_from_msg(msg)
                if method:
                    approval_seen = True
                    approval_method = method
                    request_id = rid
                    call_id = cid
                    break

        if not approval_seen:
            return ProbeResult(
                variant=variant,
                approval_seen=False,
                approval_method=None,
                call_id=None,
                request_id=None,
                command_started=False,
                command_completed=False,
                task_complete=False,
                error=None,
            )

        payloads = _build_response_payloads(variant, request_id, call_id)
        for payload in payloads:
            session.send(payload)

        # Observe whether execution unblocks.
        deadline = time.time() + post_approval_timeout_s
        while time.time() < deadline:
            for msg in session.read_messages(0.5):
                method = msg.get("method")
                params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
                if method == "codex/event/exec_command_begin":
                    payload_msg = params.get("msg") if isinstance(params.get("msg"), dict) else {}
                    if call_id is None or payload_msg.get("call_id") == call_id:
                        command_started = True
                elif method == "codex/event/exec_command_end":
                    payload_msg = params.get("msg") if isinstance(params.get("msg"), dict) else {}
                    if call_id is None or payload_msg.get("call_id") == call_id:
                        command_completed = True
                elif method in ("codex/event/task_complete", "task/complete"):
                    task_complete = True
            if command_completed:
                break
    except Exception as exc:
        error = str(exc)
    finally:
        session.close()

    return ProbeResult(
        variant=variant,
        approval_seen=approval_seen,
        approval_method=approval_method,
        call_id=call_id,
        request_id=request_id,
        command_started=command_started,
        command_completed=command_completed,
        task_complete=task_complete,
        error=error,
    )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Probe Codex app-server approval response semantics.")
    p.add_argument("--codex-bin", default=os.environ.get("CODESWARM_CODEX_BIN", "codex"))
    p.add_argument("--sandbox", default="danger-full-access" if sys.platform == "darwin" else "workspace-write")
    p.add_argument("--ask-for-approval", default="never")
    p.add_argument(
        "--scenario",
        choices=["command", "filechange", "filechange_strict", "outside_write", "network"],
        default="command",
        help="Type of approval trigger to probe.",
    )
    p.add_argument("--scenarios", default="", help="Comma-separated scenario sweep. If set, runs all listed scenarios.")
    p.add_argument("--prompt", default=DEFAULT_PROMPT)
    p.add_argument(
        "--variants",
        default="rpc_approved,rpc_accept,notify_accept,notify_approved,rpc_plus_notify",
        help="Comma-separated list of variants to test.",
    )
    p.add_argument("--pre-timeout", type=float, default=35.0, help="Seconds to wait for first approval request.")
    p.add_argument("--post-timeout", type=float, default=25.0, help="Seconds to observe completion after approval.")
    p.add_argument(
        "--force-escalation-prefix",
        action="store_true",
        help="Prepend a strict instruction that forces at least one escalated exec_command request.",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    variants = [v.strip() for v in args.variants.split(",") if v.strip()]
    scenarios = [s.strip() for s in args.scenarios.split(",") if s.strip()] if args.scenarios else [args.scenario]
    results: list[tuple[str, ProbeResult]] = []

    print("# approval-probe start", flush=True)
    print(json.dumps({"codex_bin": args.codex_bin, "variants": variants, "scenarios": scenarios}), flush=True)

    for scenario in scenarios:
        prompt = args.prompt
        if prompt == DEFAULT_PROMPT:
            prompt = SCENARIO_PROMPTS.get(scenario, DEFAULT_PROMPT)
        if args.force_escalation_prefix:
            prompt = f"{FORCE_ESCALATION_PREFIX}\n\nTask:\n{prompt}"
        print(f"# scenario {scenario}", flush=True)
        for variant in variants:
            print(f"# variant {variant}", flush=True)
            result = run_probe_variant(
                codex_bin=args.codex_bin,
                sandbox=args.sandbox,
                ask_for_approval=args.ask_for_approval,
                prompt=prompt,
                variant=variant,
                pre_approval_timeout_s=args.pre_timeout,
                post_approval_timeout_s=args.post_timeout,
            )
            results.append((scenario, result))
            result_dict = dict(result.__dict__)
            result_dict["scenario"] = scenario
            print(json.dumps(result_dict), flush=True)

    summary = {
        "completed_variants": [f"{s}:{r.variant}" for s, r in results if r.command_completed],
        "started_variants": [f"{s}:{r.variant}" for s, r in results if r.command_started],
        "approval_seen_variants": [f"{s}:{r.variant}" for s, r in results if r.approval_seen],
        "errors": [{**r.__dict__, "scenario": s} for s, r in results if r.error],
    }
    print("# approval-probe summary", flush=True)
    print(json.dumps(summary), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
