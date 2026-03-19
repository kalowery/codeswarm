#!/usr/bin/env python3
"""
Summarize approval request/response/resume flow from a Codeswarm run mailbox.

Usage:
  python3 tools/approval_flow_report.py --run-dir /path/to/run
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


APPROVAL_METHODS = {
    "codex/event/exec_approval_request",
    "codex/event/apply_patch_approval_request",
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
}

RESUME_METHODS = {
    "codex/event/exec_command_begin",
    "codex/event/exec_command_end",
    "codex/event/patch_apply_begin",
    "codex/event/patch_apply_end",
}


@dataclass
class ApprovalFlow:
    job_id: str
    node_id: int
    call_id: str
    request_methods: list[str] = field(default_factory=list)
    request_ids: list[str] = field(default_factory=list)
    turn_ids: list[str] = field(default_factory=list)
    responses_notify: int = 0
    responses_rpc: int = 0
    response_rpc_ids: list[str] = field(default_factory=list)
    resume_methods: list[str] = field(default_factory=list)

    def status(self) -> str:
        if self.resume_methods:
            return "resumed"
        if self.responses_notify or self.responses_rpc:
            return "responded_no_resume"
        return "requested_no_response"


def _jsonl_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except Exception:
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


def _extract_request(payload: dict[str, Any]) -> tuple[str | None, str | None, str | None, str | None]:
    method = payload.get("method")
    if method not in APPROVAL_METHODS:
        return (None, None, None, None)
    params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
    msg = params.get("msg") if isinstance(params.get("msg"), dict) else {}
    call_id = (
        msg.get("call_id")
        if method.startswith("codex/event/")
        else params.get("itemId")
    )
    req_id = params.get("id")
    turn_id = msg.get("turn_id") or params.get("turnId")
    return (method, str(call_id) if call_id is not None else None, str(req_id) if req_id is not None else None, str(turn_id) if turn_id is not None else None)


def _extract_resume(payload: dict[str, Any]) -> tuple[str | None, str | None]:
    method = payload.get("method")
    if method not in RESUME_METHODS:
        return (None, None)
    params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
    msg = params.get("msg") if isinstance(params.get("msg"), dict) else {}
    call_id = msg.get("call_id")
    return (method, str(call_id) if call_id is not None else None)


def _candidate_paths(run_dir: Path, sub: str) -> list[Path]:
    mailbox = run_dir / "mailbox"
    candidates: list[Path] = []
    for base in (mailbox / sub, mailbox / "archive", mailbox / "outbox", mailbox / "inbox"):
        if not base.exists() or not base.is_dir():
            continue
        candidates.extend(sorted(base.glob("*.jsonl")))
    # Deduplicate preserving order.
    seen: set[str] = set()
    out: list[Path] = []
    for p in candidates:
        s = str(p.resolve())
        if s in seen:
            continue
        seen.add(s)
        out.append(p)
    return out


def build_report(run_dir: Path) -> dict[str, Any]:
    flows: dict[tuple[str, int, str], ApprovalFlow] = {}
    by_req_id: dict[tuple[str, int, str], tuple[str, int, str]] = {}
    scanned_outbox = 0
    scanned_inbox = 0

    outbox_files = _candidate_paths(run_dir, "outbox")
    inbox_files = _candidate_paths(run_dir, "inbox")

    for path in outbox_files:
        for row in _jsonl_rows(path):
            if row.get("type") != "codex_rpc":
                continue
            scanned_outbox += 1
            payload = row.get("payload")
            if not isinstance(payload, dict):
                continue
            job_id = str(row.get("job_id", ""))
            node_id = int(row.get("node_id", -1))
            method, call_id, req_id, turn_id = _extract_request(payload)
            if method and call_id:
                key = (job_id, node_id, call_id)
                flow = flows.get(key)
                if flow is None:
                    flow = ApprovalFlow(job_id=job_id, node_id=node_id, call_id=call_id)
                    flows[key] = flow
                if method not in flow.request_methods:
                    flow.request_methods.append(method)
                if req_id and req_id not in flow.request_ids:
                    flow.request_ids.append(req_id)
                    by_req_id[(job_id, node_id, req_id)] = key
                if turn_id and turn_id not in flow.turn_ids:
                    flow.turn_ids.append(turn_id)
                continue

            resume_method, resume_call_id = _extract_resume(payload)
            if resume_method and resume_call_id:
                key = (job_id, node_id, resume_call_id)
                flow = flows.get(key)
                if flow is None:
                    flow = ApprovalFlow(job_id=job_id, node_id=node_id, call_id=resume_call_id)
                    flows[key] = flow
                if resume_method not in flow.resume_methods:
                    flow.resume_methods.append(resume_method)

    for path in inbox_files:
        for row in _jsonl_rows(path):
            scanned_inbox += 1
            if row.get("type") != "control":
                continue
            payload = row.get("payload")
            if not isinstance(payload, dict):
                continue

            # Infer job/node from filename pattern "<job>_<node>.jsonl".
            stem = path.stem
            job_id = ""
            node_id = -1
            if "_" in stem:
                left, right = stem.rsplit("_", 1)
                job_id = left
                try:
                    node_id = int(right)
                except ValueError:
                    node_id = -1

            if payload.get("method") == "exec/approvalResponse":
                params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
                call_id = params.get("call_id") or params.get("callId")
                if call_id is None:
                    continue
                key = (job_id, node_id, str(call_id))
                flow = flows.get(key)
                if flow is None:
                    flow = ApprovalFlow(job_id=job_id, node_id=node_id, call_id=str(call_id))
                    flows[key] = flow
                flow.responses_notify += 1
                continue

            if payload.get("type") == "rpc_response":
                rpc_id = payload.get("rpc_id")
                if rpc_id is None:
                    continue
                rpc_key = (job_id, node_id, str(rpc_id))
                mapped = by_req_id.get(rpc_key)
                if mapped is None:
                    # Fallback: attach to any flow with matching turn_id when rpc_id is turn id.
                    mapped = next(
                        (k for k, f in flows.items() if k[0] == job_id and k[1] == node_id and str(rpc_id) in f.turn_ids),
                        None,
                    )
                if mapped is None:
                    continue
                flow = flows[mapped]
                flow.responses_rpc += 1
                flow.response_rpc_ids.append(str(rpc_id))

    rows = sorted(flows.values(), key=lambda f: (f.job_id, f.node_id, f.call_id))
    summary = {
        "run_dir": str(run_dir),
        "outbox_files": len(outbox_files),
        "inbox_files": len(inbox_files),
        "scanned_outbox_rows": scanned_outbox,
        "scanned_inbox_rows": scanned_inbox,
        "total_flows": len(rows),
        "requested_no_response": sum(1 for f in rows if f.status() == "requested_no_response"),
        "responded_no_resume": sum(1 for f in rows if f.status() == "responded_no_resume"),
        "resumed": sum(1 for f in rows if f.status() == "resumed"),
    }
    details = [
        {
            "job_id": f.job_id,
            "node_id": f.node_id,
            "call_id": f.call_id,
            "status": f.status(),
            "request_methods": f.request_methods,
            "request_ids": f.request_ids,
            "turn_ids": f.turn_ids,
            "responses_notify": f.responses_notify,
            "responses_rpc": f.responses_rpc,
            "response_rpc_ids": f.response_rpc_ids,
            "resume_methods": f.resume_methods,
        }
        for f in rows
    ]
    return {"summary": summary, "flows": details}


def main() -> int:
    parser = argparse.ArgumentParser(description="Report approval flow correlation for a Codeswarm run mailbox.")
    parser.add_argument("--run-dir", required=True, help="Run directory containing mailbox/{inbox,outbox,archive}.")
    parser.add_argument("--json", action="store_true", help="Print full JSON report.")
    args = parser.parse_args()

    run_dir = Path(args.run_dir).expanduser().resolve()
    report = build_report(run_dir)
    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    summary = report["summary"]
    print("# approval-flow summary")
    print(json.dumps(summary, indent=2))
    print("# non-resumed flows")
    for flow in report["flows"]:
        if flow["status"] == "resumed":
            continue
        print(json.dumps(flow, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

