#!/usr/bin/env python3
"""
Probe whether Codex app-server accepts function_call_output via turn/start input.
"""

from __future__ import annotations

import argparse
import json
import os
import select
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


PROMPT = (
    "Use exec_command exactly once to run `/bin/echo FUNCTION_CALL_PROBE_OK` "
    "in the current workspace, then report the command output."
)


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
        self.thread_id: str | None = None
        self.thread_path: Path | None = None

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

    def initialize(self) -> None:
        init_id = self.send_request(
            "initialize",
            {
                "processId": None,
                "rootUri": None,
                "capabilities": {
                    "experimentalApi": True,
                },
                "clientInfo": {"name": "function-call-probe", "version": "0.1.0"},
            },
        )
        deadline = time.time() + 8
        while time.time() < deadline:
            for msg in self.read_messages(0.5):
                if msg.get("id") == init_id:
                    self.send_notification("initialized", {})
                    thread_req = self.send_request("thread/start", {})
                    thread_deadline = time.time() + 8
                    while time.time() < thread_deadline:
                        for tmsg in self.read_messages(0.5):
                            if tmsg.get("id") == thread_req and isinstance(tmsg.get("result"), dict):
                                thread = tmsg["result"].get("thread")
                                if isinstance(thread, dict):
                                    self.thread_id = thread.get("id")
                                    path = thread.get("path")
                                    if isinstance(path, str):
                                        self.thread_path = Path(path)
                                    return
        raise RuntimeError("initialize/thread-start failed")


def _tail_session_for_call(session_path: Path, timeout_s: float) -> tuple[str | None, int]:
    deadline = time.time() + timeout_s
    seen = 0
    while time.time() < deadline:
        if session_path.exists():
            lines = session_path.read_text().splitlines()
            for idx, line in enumerate(lines[seen:], start=seen):
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if row.get("type") == "response_item":
                    payload = row.get("payload", {})
                    if payload.get("type") == "function_call" and payload.get("name") == "exec_command":
                        return str(payload.get("call_id")), len(lines)
            seen = len(lines)
        time.sleep(0.1)
    return None, seen


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--codex-bin", default=os.environ.get("CODESWARM_CODEX_BIN", "codex"))
    p.add_argument("--sandbox", default="danger-full-access" if sys.platform == "darwin" else "workspace-write")
    p.add_argument("--ask-for-approval", default="never")
    p.add_argument("--prompt", default=PROMPT)
    args = p.parse_args()

    session = AppServerSession(args.codex_bin, args.sandbox, args.ask_for_approval)
    try:
        session.initialize()
        if not session.thread_id or not session.thread_path:
            raise RuntimeError("missing thread metadata")

        session.send_request(
            "turn/start",
            {
                "threadId": session.thread_id,
                "input": [{"type": "text", "text": args.prompt}],
            },
        )

        call_id, seen = _tail_session_for_call(session.thread_path, timeout_s=20.0)
        print(json.dumps({"stage": "function_call_seen", "call_id": call_id, "thread_path": str(session.thread_path)}), flush=True)
        if not call_id:
            return 1

        session.send_request(
            "turn/start",
            {
                "threadId": session.thread_id,
                "input": [
                    {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": "FUNCTION_CALL_PROBE_OUTPUT",
                    }
                ],
            },
        )

        deadline = time.time() + 15.0
        while time.time() < deadline:
            msgs = session.read_messages(0.5)
            for msg in msgs:
                print(json.dumps(msg), flush=True)
            if session.thread_path.exists():
                lines = session.thread_path.read_text().splitlines()
                for line in lines[seen:]:
                    try:
                        row = json.loads(line)
                    except Exception:
                        continue
                    if row.get("type") == "response_item" and row.get("payload", {}).get("type") == "function_call_output":
                        print(json.dumps({"stage": "function_call_output_seen", "payload": row.get("payload")}), flush=True)
                        return 0
                seen = len(lines)
        return 2
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
