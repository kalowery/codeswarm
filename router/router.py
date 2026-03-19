import argparse
import subprocess
import json
import sys
import uuid
import shlex
import os
import select
import copy
from pathlib import Path
from datetime import datetime, timezone
import threading
import re
import time
import atexit
from collections import defaultdict, deque

sys.path.append(str(Path(__file__).resolve().parents[1]))
from common.config import load_config
from .providers.factory import build_providers, get_provider_specs


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
RESOLVED_APPROVALS = {}
RECENT_APPROVAL_RESULTS = {}
APPROVALS_VERSION = 0
ACTIVE_PROVIDER = None
PROVIDERS = {}
PROVIDER_SPECS = []
NODE_OUTSTANDING = defaultdict(int)
NODE_THREAD_ACTIVE = defaultdict(bool)
FINAL_ANSWER_SEEN = set()
INTER_SWARM_QUEUE = defaultdict(deque)
SCHEDULER_LOCK = threading.Lock()
TERMINATION_IN_PROGRESS = set()
FORCE_TERMINATION_REQUESTED = set()

# Retention policy
TERMINATED_TTL_SECONDS = 900  # 15 minutes
MAX_TERMINATED = 100

STATE_FILE = Path(__file__).resolve().parents[1] / "router_state.json"
PID_FILE = Path(__file__).resolve().parents[1] / "router.pid"
DEFAULT_AGENTS_FILE = Path(__file__).resolve().parents[1] / "AGENTS.md"
GRACEFUL_TERMINATE_TIMEOUT_SECONDS = 300
GRACEFUL_TERMINATE_POLL_SECONDS = 0.5
RESOLVED_APPROVAL_TTL_SECONDS = 180
APPROVAL_ACK_RETRY_SECONDS = 2.0
# Keep retrying approval decisions until command begin/end (or filechange begin/end)
# confirms delivery. Short outages can exceed fixed retry windows and otherwise
# deadlock a turn until a new prompt re-triggers approval flow.
APPROVAL_ACK_MAX_BACKOFF_SECONDS = 15.0


def _bump_approvals_version():
    global APPROVALS_VERSION
    APPROVALS_VERSION += 1
    return APPROVALS_VERSION


def _normalize_agents_text(value):
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text if text else None


def _load_default_agents_md():
    try:
        return _normalize_agents_text(DEFAULT_AGENTS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def _merge_agents_md(default_md, user_md):
    base = _normalize_agents_text(default_md)
    user = _normalize_agents_text(user_md)
    if not base:
        return user
    if not user:
        return base
    return f"{base}\n\n{user}"


def _normalize_node_id(value):
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _approval_key(job_id, call_id, node_id):
    return (str(job_id), _normalize_node_id(node_id), str(call_id))


def _is_jsonrpc_id(value):
    if value is None:
        return False
    if isinstance(value, str):
        return True
    # bool is a subclass of int in Python; exclude it explicitly.
    if isinstance(value, bool):
        return False
    return isinstance(value, (int, float))


def _same_jsonrpc_id(left, right):
    if not _is_jsonrpc_id(left) or not _is_jsonrpc_id(right):
        return False
    return type(left) is type(right) and left == right


def _to_appserver_approval_decision(decision, approved_flag):
    """
    Normalize approval decisions to documented app-server vocabulary:
      - accept
      - acceptForSession
      - decline
      - cancel
      - {acceptWithExecpolicyAmendment:{execpolicy_amendment:[...]}}
    """
    if isinstance(decision, dict):
        if isinstance(decision.get("acceptWithExecpolicyAmendment"), dict):
            amendment = decision["acceptWithExecpolicyAmendment"].get("execpolicy_amendment")
            if isinstance(amendment, list):
                return {
                    "acceptWithExecpolicyAmendment": {
                        "execpolicy_amendment": amendment
                    }
                }
        # Back-compat: translate legacy/native amendment form.
        if isinstance(decision.get("approved_execpolicy_amendment"), dict):
            amendment = decision["approved_execpolicy_amendment"].get("proposed_execpolicy_amendment")
            if isinstance(amendment, list):
                return {
                    "acceptWithExecpolicyAmendment": {
                        "execpolicy_amendment": amendment
                    }
                }
        return "accept" if bool(approved_flag) else "decline"

    if isinstance(decision, str):
        if bool(approved_flag):
            if decision in ("decline", "cancel", "abort"):
                return "accept"
        else:
            if decision in ("accept", "acceptForSession", "approved"):
                return "decline"
        if decision in ("accept", "acceptForSession", "decline", "cancel"):
            return decision
        # Back-compat aliases seen in existing traces.
        if decision == "approved":
            return "accept"
        if decision == "abort":
            return "decline"

    return "accept" if bool(approved_flag) else "decline"


def _approval_extract_amendment_from_decision(decision, proposed_execpolicy_amendment=None):
    if not isinstance(decision, dict):
        if isinstance(proposed_execpolicy_amendment, list):
            return proposed_execpolicy_amendment
        return None
    if isinstance(decision.get("approved_execpolicy_amendment"), dict):
        inner = decision["approved_execpolicy_amendment"]
        if isinstance(inner.get("proposed_execpolicy_amendment"), list):
            return inner["proposed_execpolicy_amendment"]
    if isinstance(decision.get("acceptWithExecpolicyAmendment"), dict):
        inner = decision["acceptWithExecpolicyAmendment"]
        if isinstance(inner.get("execpolicy_amendment"), list):
            return inner["execpolicy_amendment"]
    if isinstance(proposed_execpolicy_amendment, list):
        return proposed_execpolicy_amendment
    return None


def _approval_available_flags(available):
    flags = {
        "accept": False,
        "cancel": False,
        "approved": False,
        "approved_for_session": False,
        "denied": False,
        "abort": False,
        "accept_with_amendment": False,
        "approved_with_amendment": False,
    }
    for entry in (available or []):
        if isinstance(entry, str):
            if entry in flags:
                flags[entry] = True
        elif isinstance(entry, dict):
            if "acceptWithExecpolicyAmendment" in entry:
                flags["accept_with_amendment"] = True
            if "approved_execpolicy_amendment" in entry:
                flags["approved_with_amendment"] = True
    return flags


def _normalize_decision_for_available(
    decision,
    approved_flag,
    available,
    prefer_native_dialect=False,
    proposed_execpolicy_amendment=None,
):
    flags = _approval_available_flags(available)
    amendment = _approval_extract_amendment_from_decision(
        decision,
        proposed_execpolicy_amendment,
    )

    def _approved_plain():
        if prefer_native_dialect:
            if flags["approved"]:
                return "approved"
            if flags["approved_for_session"]:
                return "approved_for_session"
            return "approved" if approved_flag else "abort"
        if flags["accept"]:
            return "accept"
        if flags["approved"]:
            return "approved"
    return "accept" if approved_flag else "cancel"


def _choose_richer_approval_command(previous, new):
    if previous is None:
        return new
    if new is None:
        return previous

    def _change_count(command):
        if not isinstance(command, dict):
            return 0
        changes = command.get("changes")
        if isinstance(changes, list):
            return len(changes)
        if isinstance(changes, dict):
            files = changes.get("files")
            nested = changes.get("changes")
            return max(
                len(files) if isinstance(files, list) else 0,
                len(nested) if isinstance(nested, list) else 0,
                len(changes),
            )
        return 0

    prev_count = _change_count(previous)
    new_count = _change_count(new)
    if new_count > prev_count:
        return new
    if prev_count > new_count:
        return previous
    if new_count > 0 and prev_count > 0:
        return new if len(json.dumps(new, ensure_ascii=False)) >= len(json.dumps(previous, ensure_ascii=False)) else previous
    return new


def _parse_apply_patch_command_from_input(patch_input):
    if not isinstance(patch_input, str) or not patch_input.strip():
        return "Apply file changes"

    changes = []
    current = None

    def _finish_current():
        if current is None:
            return
        diff_lines = current.pop("_diff_lines", [])
        diff_text = "\n".join(diff_lines).strip()
        if diff_text:
            current["diff"] = diff_text
        changes.append(current)

    for raw_line in patch_input.splitlines():
        line = raw_line.rstrip("\n")
        if line.startswith("*** Add File: "):
            _finish_current()
            current = {
                "path": line[len("*** Add File: "):].strip(),
                "kind": {"type": "add"},
                "_diff_lines": [],
            }
            continue
        if line.startswith("*** Update File: "):
            _finish_current()
            current = {
                "path": line[len("*** Update File: "):].strip(),
                "kind": {"type": "update"},
                "_diff_lines": [],
            }
            continue
        if line.startswith("*** Delete File: "):
            _finish_current()
            current = {
                "path": line[len("*** Delete File: "):].strip(),
                "kind": {"type": "delete"},
                "_diff_lines": [],
            }
            continue
        if line.startswith("*** Move to: "):
            if current is not None:
                current["to"] = line[len("*** Move to: "):].strip()
                current["kind"] = {"type": "move"}
                current.setdefault("_diff_lines", []).append(line)
            continue
        if line.startswith("*** End Patch"):
            break
        if current is not None:
            current.setdefault("_diff_lines", []).append(line)

    _finish_current()
    if not changes:
        return "Apply file changes"
    return {"changes": changes}

    def _denied_plain():
        if prefer_native_dialect:
            if flags["denied"]:
                return "denied"
            if flags["abort"]:
                return "abort"
            return "abort" if not approved_flag else "approved"
        if flags["cancel"]:
            return "cancel"
        if flags["abort"]:
            return "abort"
        return "cancel" if not approved_flag else "accept"

    if approved_flag:
        if isinstance(decision, str):
            if prefer_native_dialect:
                if decision == "accept":
                    decision = "approved"
                elif decision == "cancel":
                    decision = "abort"
                elif decision == "acceptForSession":
                    decision = "approved_for_session"
            if decision in ("accept", "approved", "approved_for_session") and flags.get(decision, False):
                return decision
            if decision == "cancel" and flags["cancel"]:
                return "cancel"
            if decision == "denied" and flags["denied"]:
                return "denied"
            if decision == "abort" and flags["abort"]:
                return "abort"

        if (
            prefer_native_dialect
            and isinstance(decision, dict)
            and isinstance(decision.get("acceptWithExecpolicyAmendment"), dict)
        ):
            native_amendment = decision["acceptWithExecpolicyAmendment"].get("execpolicy_amendment")
            if isinstance(native_amendment, list):
                return {
                    "approved_execpolicy_amendment": {
                        "proposed_execpolicy_amendment": native_amendment
                    }
                }

        if amendment:
            if prefer_native_dialect and flags["approved_with_amendment"]:
                return {
                    "approved_execpolicy_amendment": {
                        "proposed_execpolicy_amendment": amendment
                    }
                }
            if flags["accept_with_amendment"]:
                return {
                    "acceptWithExecpolicyAmendment": {
                        "execpolicy_amendment": amendment
                    }
                }
            if flags["approved_with_amendment"]:
                return {
                    "approved_execpolicy_amendment": {
                        "proposed_execpolicy_amendment": amendment
                    }
                }
        return _approved_plain()

    if isinstance(decision, str):
        if prefer_native_dialect:
            if decision == "accept":
                decision = "approved"
            elif decision == "cancel":
                decision = "abort"
            elif decision == "decline":
                decision = "denied"
        if decision in ("cancel", "abort", "denied") and flags.get(decision, False):
            return decision
        if decision == "accept" and flags["accept"]:
            return "accept"
        if decision == "approved" and flags["approved"]:
            return "approved"
    return _denied_plain()


def _coerce_to_advertised_decision(candidate, available, approved_flag, prefer_native_dialect=False):
    def _decision_token(value):
        if isinstance(value, str):
            return ("s", value)
        if isinstance(value, dict):
            try:
                return ("j", json.dumps(value, sort_keys=True))
            except Exception:
                return ("o", str(value))
        return ("x", str(value))

    advertised = list(available or [])
    if not advertised:
        return candidate

    advertised_keys = {_decision_token(entry) for entry in advertised}
    if _decision_token(candidate) in advertised_keys:
        return candidate

    if bool(approved_flag):
        preferred = (
            ("approved", "approved_for_session", "accept", "acceptForSession")
            if prefer_native_dialect
            else ("accept", "approved", "acceptForSession")
        )
    else:
        preferred = (
            ("denied", "abort", "cancel", "decline")
            if prefer_native_dialect
            else ("cancel", "decline", "abort")
        )

    for token in preferred:
        if token in advertised:
            return token

    if bool(approved_flag):
        for entry in advertised:
            if isinstance(entry, dict):
                return entry

    return advertised[0]


def _approval_lookup(job_id, call_id, node_id=None, injection_id=None):
    norm_job_id = str(job_id)
    norm_call_id = str(call_id)
    norm_node_id = _normalize_node_id(node_id)

    if norm_node_id is not None:
        direct_key = (norm_job_id, norm_node_id, norm_call_id)
        meta = PENDING_APPROVALS.get(direct_key)
        if meta:
            return direct_key, meta

    matches = []
    for key, meta in PENDING_APPROVALS.items():
        if key[0] != norm_job_id or key[2] != norm_call_id:
            continue
        matches.append((key, meta))

    if not matches:
        return (None, None)

    if injection_id is not None:
        norm_injection_id = str(injection_id)
        by_injection = [
            (key, meta)
            for key, meta in matches
            if str(meta.get("injection_id")) == norm_injection_id
        ]
        if len(by_injection) == 1:
            return by_injection[0]
        if len(by_injection) > 1:
            # Ambiguous across nodes even with injection id; do not guess.
            return (None, None)

    if len(matches) == 1:
        # If caller provided a node_id, do not route to another node.
        if norm_node_id is not None and matches[0][0][1] != norm_node_id:
            return (None, None)
        return matches[0]

    # Ambiguous call_id across nodes; do not guess.
    return (None, None)


def _mark_approval_resolved(job_id, call_id, node_id):
    key = _approval_key(job_id, call_id, node_id)
    RESOLVED_APPROVALS[key] = time.time()


def _remember_recent_approval_result(job_id, call_id, node_id, approved, decision):
    key = _approval_key(job_id, call_id, node_id)
    RECENT_APPROVAL_RESULTS[key] = {
        "approved": bool(approved),
        "decision": decision,
        "ts": time.time(),
    }


def _get_recent_approval_result(job_id, call_id, node_id):
    now = time.time()
    expired = [
        key
        for key, meta in RECENT_APPROVAL_RESULTS.items()
        if (now - float(meta.get("ts", 0))) > RESOLVED_APPROVAL_TTL_SECONDS
    ]
    for key in expired:
        RECENT_APPROVAL_RESULTS.pop(key, None)
    return RECENT_APPROVAL_RESULTS.get(_approval_key(job_id, call_id, node_id))


def _is_recently_resolved_approval(job_id, call_id, node_id):
    now = time.time()
    expired = [
        key
        for key, ts in RESOLVED_APPROVALS.items()
        if (now - float(ts)) > RESOLVED_APPROVAL_TTL_SECONDS
    ]
    for key in expired:
        RESOLVED_APPROVALS.pop(key, None)
    key = _approval_key(job_id, call_id, node_id)
    ts = RESOLVED_APPROVALS.get(key)
    if ts is None:
        return False
    return (now - float(ts)) <= RESOLVED_APPROVAL_TTL_SECONDS


def _send_duplicate_approval_replay(
    provider,
    job_id,
    node_id,
    call_id,
    rpc_id,
    request_id_hint,
    approved,
    decision,
    approval_method=None,
    has_native_approval=False,
    turn_id=None,
):
    # Replay resolved approvals using the exact stored decision token/object.
    try:
        replay_decision = decision
        if replay_decision is None:
            replay_decision = "approved" if bool(approved) else "abort"

        sent_rpc = False
        if rpc_id is not None:
            provider.send_control(
                job_id,
                node_id,
                {
                    "type": "rpc_response",
                    "rpc_id": rpc_id,
                    "result": {
                        "decision": replay_decision,
                    },
                },
            )
            sent_rpc = True

        # Also send notification-style fallback for maximum runtime compatibility.
        notify_decision = _to_appserver_approval_decision(replay_decision, bool(approved))

        params_payload = {
            "call_id": call_id,
            "callId": call_id,
            "approved": bool(approved),
            "decision": notify_decision,
        }
        if isinstance(turn_id, (str, int)):
            params_payload["id"] = turn_id
            params_payload["turn_id"] = turn_id
            params_payload["turnId"] = turn_id
        if isinstance(notify_decision, dict):
            params_payload.update(notify_decision)
        provider.send_control(
            job_id,
            node_id,
            {
                "method": "exec/approvalResponse",
                "params": params_payload,
            },
        )
        # Do not emit fallback rpc_response for notification-style approvals.
        # Only the top-level JSON-RPC id is a transport correlation id.
    except Exception:
        return False
    return True


def _prune_pending_approval_for_call(job_id, node_id, call_id, source):
    key, meta = _approval_lookup(job_id, call_id, node_id=node_id)
    if not key:
        return False
    pending_decision = PENDING_APPROVAL_DECISIONS.pop(key, None)
    PENDING_APPROVALS.pop(key, None)
    if pending_decision:
        _remember_recent_approval_result(
            job_id,
            call_id,
            node_id,
            bool(pending_decision.get("approved")),
            pending_decision.get("decision"),
        )
    _mark_approval_resolved(job_id, call_id, node_id)
    approvals_version = _bump_approvals_version()
    approval_trace(
        "pending_pruned_by_command_event",
        source=source,
        job_id=str(job_id),
        node_id=_normalize_node_id(node_id),
        call_id=str(call_id),
        approval_id=(meta or {}).get("approval_id"),
        approvals_version=approvals_version,
    )
    if pending_decision:
        emit_event("exec_approval_resolved", {
            "request_id": pending_decision.get("request_id"),
            "approval_id": (meta or {}).get("approval_id"),
            "job_id": str(job_id),
            "call_id": str(call_id),
            "node_id": _normalize_node_id(node_id),
            "injection_id": (meta or {}).get("injection_id"),
            "approved": bool(pending_decision.get("approved")),
            "decision": pending_decision.get("decision"),
            "source": source,
            "approvals_version": approvals_version,
        })
    return True


def _pending_approvals_snapshot():
    snapshot = {}
    # Snapshot keys first so iteration remains stable even while new
    # approvals arrive from worker streams.
    items = list(PENDING_APPROVALS.items())

    for key, meta in items:
        if not isinstance(key, tuple) or len(key) != 3:
            continue
        if not isinstance(meta, dict):
            continue

        job_id, node_id, call_id = key
        norm_node_id = _normalize_node_id(node_id)
        if norm_node_id is None:
            continue

        swarm_id = meta.get("swarm_id") or JOB_TO_SWARM.get(str(job_id))
        if not swarm_id:
            continue

        created_at_ts = meta.get("created_at_ts")
        try:
            created_at_ms = int(float(created_at_ts) * 1000) if created_at_ts is not None else int(time.time() * 1000)
        except Exception:
            created_at_ms = int(time.time() * 1000)

        approval_status = str(meta.get("approval_status") or "pending")
        approval_method = str(meta.get("approval_method") or "")
        is_session_derived = approval_method.startswith("session/")
        # Keep submitted approvals visible until explicit progress or resolution
        # arrives for that exact call_id. Hiding native approvals immediately on
        # submit causes dialogs to flash/disappear before the runtime actually
        # consumes the approval, which is especially confusing during approval
        # races or follower lag.
        if approval_status == "resolved":
            continue

        approval = {
            "job_id": str(job_id),
            "approval_id": meta.get("approval_id"),
            "approval_status": approval_status,
            "call_id": str(call_id),
            "injection_id": meta.get("injection_id"),
            "turn_id": meta.get("turn_id"),
            "command": meta.get("command"),
            "reason": meta.get("reason"),
            "cwd": meta.get("cwd"),
            "proposed_execpolicy_amendment": meta.get("proposed_execpolicy_amendment"),
            "available_decisions": meta.get("available_decisions"),
            "created_at_ms": created_at_ms,
            "updated_at_ms": int(float(meta.get("updated_at_ts") or created_at_ts or time.time()) * 1000),
        }

        node_map = snapshot.setdefault(str(swarm_id), {})
        node_list = node_map.setdefault(norm_node_id, [])
        node_list.append(approval)

    for node_map in snapshot.values():
        for approvals in node_map.values():
            approvals.sort(key=lambda a: int(a.get("created_at_ms", 0)))

    return snapshot


def _send_approval_decision(
    provider,
    job_id,
    node_id,
    control_payload,
    compat_rpc_payload=None,
    compat_notify_payload=None,
):
    if not isinstance(control_payload, dict):
        raise RuntimeError("missing approval control payload")
    provider.send_control(job_id, node_id, control_payload)
    if compat_rpc_payload is not None:
        provider.send_control(job_id, node_id, compat_rpc_payload)
    if compat_notify_payload is not None:
        provider.send_control(job_id, node_id, compat_notify_payload)


def _retry_unacked_approvals():
    now = time.time()
    for key, meta in list(PENDING_APPROVALS.items()):
        if not isinstance(meta, dict):
            continue
        status = str(meta.get("approval_status") or "")
        if status not in ("approved_pending_ack", "denied_pending_ack"):
            continue

        pending = PENDING_APPROVAL_DECISIONS.get(key)
        if not isinstance(pending, dict):
            continue

        approval_method = str(meta.get("approval_method") or "")
        # Implicit file-change approvals are synthesized from item/started when
        # app-server never emitted a real requestApproval event. There is no
        # request id to correlate against, and repeated exec/approvalResponse
        # notifications do not unblock Codex. Skip retries for this fallback so
        # the UI can time out quickly instead of appearing hung.
        if approval_method == "item/fileChange/implicitFromStarted":
            continue

        attempts = int(pending.get("send_attempts", 0) or 0)
        # Exponential-ish backoff with cap; do not stop retrying.
        retry_after = min(
            APPROVAL_ACK_RETRY_SECONDS * max(1.0, float(2 ** min(attempts // 4, 6))),
            APPROVAL_ACK_MAX_BACKOFF_SECONDS,
        )
        last_sent = float(pending.get("last_sent_ts", 0.0) or 0.0)
        if (now - last_sent) < retry_after:
            continue

        swarm_id = meta.get("swarm_id")
        provider = _provider_for_swarm(swarm_id)
        if not provider:
            continue

        try:
            control_payload = pending.get("control_payload") or {}
            if (
                pending.get("compat_notify_payload") is None
                and isinstance(control_payload, dict)
                and control_payload.get("type") == "rpc_response"
            ):
                approved_flag = bool(pending.get("approved"))
                route_available_decisions = (
                    meta.get("available_decisions")
                    if isinstance(meta.get("available_decisions"), list)
                    else []
                )
                notify_decision = _normalize_decision_for_available(
                    pending.get("decision"),
                    approved_flag,
                    route_available_decisions,
                    False,
                )
                notify_params = {
                    "call_id": key[2],
                    "callId": key[2],
                    "approved": approved_flag,
                    "decision": notify_decision,
                }
                if isinstance(notify_decision, dict):
                    notify_params.update(notify_decision)
                pending["compat_notify_payload"] = {
                    "method": "exec/approvalResponse",
                    "params": notify_params,
                }
                approval_trace(
                    "approve_command_retry_notify_synthesized",
                    job_id=str(pending.get("job_id")),
                    node_id=_normalize_node_id(pending.get("node_id")),
                    call_id=str(key[2]),
                    approval_id=meta.get("approval_id"),
                )

            _send_approval_decision(
                provider,
                pending.get("job_id"),
                pending.get("node_id"),
                pending.get("control_payload"),
                pending.get("compat_rpc_payload"),
                pending.get("compat_notify_payload"),
            )
            pending["send_attempts"] = attempts + 1
            pending["last_sent_ts"] = now
            meta["updated_at_ts"] = now
            route_mode = "rpc_response" if control_payload.get("type") == "rpc_response" else "exec_approval_response"
            fallback_notify = pending.get("compat_notify_payload") is not None
            # Structured retry-route diagnostics for approval correlation.
            if pending["send_attempts"] <= 3 or (pending["send_attempts"] % 10 == 0):
                emit_event("debug", {
                    "source": "router",
                    "message": (
                        "approval retry route="
                        f"{route_mode} job_id={pending.get('job_id')} node_id={pending.get('node_id')} "
                        f"call_id={key[2]} rpc_id={control_payload.get('rpc_id')} "
                        f"compat_rpc_id={(pending.get('compat_rpc_payload') or {}).get('rpc_id')} "
                        f"fallback_notify={fallback_notify} attempt={pending['send_attempts']}"
                    ),
                })
            if pending["send_attempts"] % 20 == 0:
                emit_event("debug", {
                    "source": "router",
                    "message": (
                        "approval delivery still awaiting ack "
                        f"job_id={pending.get('job_id')} node_id={pending.get('node_id')} "
                        f"call_id={key[2]} attempts={pending['send_attempts']}"
                    ),
                })
            approval_trace(
                "approve_command_retry_sent",
                job_id=str(pending.get("job_id")),
                node_id=_normalize_node_id(pending.get("node_id")),
                call_id=str(key[2]),
                approval_id=meta.get("approval_id"),
                send_attempts=pending["send_attempts"],
                fallback_notify=fallback_notify,
            )
        except Exception as e:
            approval_trace(
                "approve_command_retry_error",
                job_id=str(pending.get("job_id")),
                node_id=_normalize_node_id(pending.get("node_id")),
                call_id=str(key[2]),
                approval_id=meta.get("approval_id"),
                send_attempts=attempts,
                error=str(e),
            )


def save_state():
    try:
        # Snapshot mutable in-memory structures while holding the scheduler lock
        # so serialization cannot race with concurrent updates.
        with SCHEDULER_LOCK:
            swarms_snapshot = copy.deepcopy(SWARMS)
            queue_snapshot = []
            for target_swarm_id, q in INTER_SWARM_QUEUE.items():
                for item in q:
                    queue_snapshot.append({
                        "queue_id": item.get("queue_id"),
                        "request_id": item.get("request_id"),
                        "source_swarm_id": item.get("source_swarm_id"),
                        "target_swarm_id": str(target_swarm_id),
                        "selector": item.get("selector"),
                        "nodes": item.get("nodes"),
                        "content": item.get("content"),
                        "created_at": item.get("created_at"),
                    })

        data = {
            "swarms": swarms_snapshot,
            "inter_swarm_queue": queue_snapshot,
        }

        tmp_path = STATE_FILE.with_name(f"{STATE_FILE.name}.tmp")
        with open(tmp_path, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, STATE_FILE)
    except Exception as e:
        print(f"[router ERROR] failed to save state to {STATE_FILE}: {e}", file=sys.stderr, flush=True)


def load_state():
    global SWARMS, INTER_SWARM_QUEUE
    try:
        if STATE_FILE.exists():
            with open(STATE_FILE) as f:
                data = json.load(f)
                SWARMS = data.get("swarms", {})
                restored_queue = defaultdict(deque)
                for item in data.get("inter_swarm_queue", []):
                    if not isinstance(item, dict):
                        continue
                    target_swarm_id = item.get("target_swarm_id")
                    if not target_swarm_id:
                        continue
                    restored_queue[str(target_swarm_id)].append({
                        "queue_id": item.get("queue_id"),
                        "request_id": item.get("request_id"),
                        "source_swarm_id": item.get("source_swarm_id"),
                        "target_swarm_id": str(target_swarm_id),
                        "selector": item.get("selector") or "idle",
                        "nodes": item.get("nodes"),
                        "content": item.get("content"),
                        "created_at": item.get("created_at"),
                    })
                INTER_SWARM_QUEUE = restored_queue
    except Exception:
        SWARMS = {}
        INTER_SWARM_QUEUE = defaultdict(deque)


def write_pid_file():
    try:
        PID_FILE.write_text(f"{os.getpid()}\n", encoding="utf-8")
    except Exception:
        pass


def remove_pid_file():
    try:
        if PID_FILE.exists():
            raw = PID_FILE.read_text(encoding="utf-8").strip()
            if raw == str(os.getpid()):
                PID_FILE.unlink()
    except Exception:
        pass


def reconcile(providers):
    global JOB_TO_SWARM
    running_jobs_by_provider = {}
    for provider_ref, provider in providers.items():
        try:
            running_jobs_by_provider[provider_ref] = provider.list_active_jobs()
        except Exception:
            running_jobs_by_provider[provider_ref] = {}

    JOB_TO_SWARM.clear()

    to_remove = []

    for swarm_id, swarm in SWARMS.items():
        provider_ref = swarm.get("provider") or swarm.get("backend")
        if not provider_ref and providers:
            provider_ref = next(iter(providers.keys()))
            swarm["provider"] = provider_ref

        job_id = swarm.get("job_id")
        running_jobs = running_jobs_by_provider.get(str(provider_ref), {})
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


def _to_int(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _normalize_usage_payload(base, total_usage, last_usage, model_context_window, source_method):
    total_tokens = _to_int((total_usage or {}).get("total_tokens"))
    input_tokens = _to_int((total_usage or {}).get("input_tokens"))
    cached_input_tokens = _to_int((total_usage or {}).get("cached_input_tokens"))
    output_tokens = _to_int((total_usage or {}).get("output_tokens"))
    reasoning_output_tokens = _to_int((total_usage or {}).get("reasoning_output_tokens"))

    last_total_tokens = _to_int((last_usage or {}).get("total_tokens"))
    last_input_tokens = _to_int((last_usage or {}).get("input_tokens"))
    last_cached_input_tokens = _to_int((last_usage or {}).get("cached_input_tokens"))
    last_output_tokens = _to_int((last_usage or {}).get("output_tokens"))
    last_reasoning_output_tokens = _to_int((last_usage or {}).get("reasoning_output_tokens"))

    if total_tokens is None:
        return None

    return {
        **base,
        # Backward-compatible top-level total used by existing UI.
        "total_tokens": total_tokens,
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "output_tokens": output_tokens,
        "reasoning_output_tokens": reasoning_output_tokens,
        "last_total_tokens": last_total_tokens,
        "last_input_tokens": last_input_tokens,
        "last_cached_input_tokens": last_cached_input_tokens,
        "last_output_tokens": last_output_tokens,
        "last_reasoning_output_tokens": last_reasoning_output_tokens,
        "model_context_window": _to_int(model_context_window),
        "usage_source": source_method,
    }


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


def approval_trace(stage, **fields):
    def _compact(value, depth=0):
        if value is None:
            return value
        if isinstance(value, str):
            return value if len(value) <= 240 else (value[:240] + "…")
        if isinstance(value, (int, float, bool)):
            return value
        if depth >= 3:
            return "[truncated]"
        if isinstance(value, list):
            limit = 8
            out = [_compact(item, depth + 1) for item in value[:limit]]
            if len(value) > limit:
                out.append(f"[+{len(value) - limit} more]")
            return out
        if isinstance(value, dict):
            return {str(k): _compact(v, depth + 1) for k, v in value.items()}
        return str(value)

    payload = {"stage": str(stage), "ts": time.time(), **_compact(fields)}
    try:
        print(f"[router APPROVAL] {json.dumps(payload, sort_keys=True)}", flush=True)
    except Exception:
        # Best-effort diagnostic output should never break routing.
        pass


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


def _provider_for_id(provider_id):
    if provider_id is None:
        return None
    return PROVIDERS.get(str(provider_id))


def _provider_for_swarm(swarm_id):
    swarm = SWARMS.get(str(swarm_id))
    if not swarm:
        return None
    provider_id = swarm.get("provider")
    return _provider_for_id(provider_id)


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


def _is_node_quiescent(swarm_id, node_id):
    key = _node_key(swarm_id, node_id)
    outstanding = int(NODE_OUTSTANDING.get(key, 0))
    active = bool(NODE_THREAD_ACTIVE.get(key, False))
    return outstanding == 0 and not active


def _is_swarm_quiescent(swarm_id):
    swarm = SWARMS.get(str(swarm_id))
    if not swarm:
        return True
    node_count = int(swarm.get("node_count") or 0)
    with SCHEDULER_LOCK:
        return all(_is_node_quiescent(swarm_id, node_id) for node_id in range(node_count))


def _is_swarm_quiescent_for_termination(swarm_id):
    """
    Termination quiescence is keyed on real queued/in-flight work (outstanding).
    If a node is marked active but has no outstanding work, treat it as stale and
    clear the active flag so shutdown is not delayed needlessly.
    """
    swarm = SWARMS.get(str(swarm_id))
    if not swarm:
        return True
    node_count = int(swarm.get("node_count") or 0)
    with SCHEDULER_LOCK:
        for node_id in range(node_count):
            key = _node_key(swarm_id, node_id)
            outstanding = int(NODE_OUTSTANDING.get(key, 0))
            if outstanding > 0:
                return False
            if bool(NODE_THREAD_ACTIVE.get(key, False)):
                NODE_THREAD_ACTIVE[key] = False
        return True


def _finalize_swarm_termination(provider, request_id, swarm_id, job_id):
    swarm = SWARMS.get(str(swarm_id))
    if not swarm:
        return

    if swarm.get("status") != "terminated":
        swarm["status"] = "terminated"
        swarm["terminated_at"] = time.time()
    swarm.pop("terminating_since", None)

    with SCHEDULER_LOCK:
        node_count = int(swarm.get("node_count") or 0)
        for node_id in range(node_count):
            key = _node_key(swarm_id, node_id)
            NODE_THREAD_ACTIVE.pop(key, None)
            NODE_OUTSTANDING.pop(key, None)
        stale_final_keys = [
            k for k in FINAL_ANSWER_SEEN
            if isinstance(k, tuple) and len(k) == 3 and str(k[0]) == str(swarm_id)
        ]
        for key in stale_final_keys:
            FINAL_ANSWER_SEEN.discard(key)

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

    # User-terminated swarms should be removed from persisted active state
    # immediately (not retained for TTL).
    SWARMS.pop(str(swarm_id), None)
    if job_id:
        JOB_TO_SWARM.pop(str(job_id), None)
        stale_approvals = [
            key for key in list(PENDING_APPROVALS.keys())
            if isinstance(key, tuple) and len(key) == 3 and str(key[0]) == str(job_id)
        ]
        removed_any_approval = False
        for key in stale_approvals:
            PENDING_APPROVALS.pop(key, None)
            PENDING_APPROVAL_DECISIONS.pop(key, None)
            RESOLVED_APPROVALS.pop(key, None)
            RECENT_APPROVAL_RESULTS.pop(key, None)
            removed_any_approval = True
        if removed_any_approval:
            _bump_approvals_version()
    save_state()


def _maybe_export_workspace_archive(config, provider, request_id, swarm_id, job_id, terminate_params):
    if not isinstance(terminate_params, dict):
        return
    if not bool(terminate_params.get("download_workspaces_on_shutdown", False)):
        return
    if not hasattr(provider, "create_workspace_archive"):
        return

    archive_root = (
        config.get("router", {}).get("download_archive_root")
        if isinstance(config.get("router"), dict)
        else None
    )
    if not isinstance(archive_root, str) or not archive_root.strip():
        archive_root = "/tmp/codeswarm-downloads"

    output_dir = Path(str(archive_root)).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        archive_path = provider.create_workspace_archive(str(job_id), str(swarm_id), output_dir)
    except Exception as e:
        emit_event("workspace_archive_failed", {
            "request_id": request_id,
            "swarm_id": swarm_id,
            "job_id": job_id,
            "reason": str(e),
        })
        return

    if not archive_path:
        emit_event("workspace_archive_failed", {
            "request_id": request_id,
            "swarm_id": swarm_id,
            "job_id": job_id,
            "reason": "No workspace artifacts found to archive",
        })
        return

    emit_event("workspace_archive_ready", {
        "request_id": request_id,
        "swarm_id": swarm_id,
        "job_id": job_id,
        "archive_path": str(archive_path),
        "archive_name": Path(str(archive_path)).name,
    })


def _graceful_terminate_swarm(config, provider, request_id, swarm_id, terminate_overrides=None):
    swarm = SWARMS.get(str(swarm_id))
    if not swarm:
        emit_event("command_rejected", {
            "request_id": request_id,
            "reason": "unknown swarm_id"
        })
        return

    job_id = swarm.get("job_id")
    if not job_id:
        _finalize_swarm_termination(provider, request_id, swarm_id, job_id)
        with SCHEDULER_LOCK:
            TERMINATION_IN_PROGRESS.discard(str(swarm_id))
        return

    timeout_s = (
        config.get("router", {}).get("graceful_terminate_timeout_seconds")
        if isinstance(config.get("router"), dict)
        else None
    )
    if not isinstance(timeout_s, (int, float)) or timeout_s <= 0:
        timeout_s = GRACEFUL_TERMINATE_TIMEOUT_SECONDS

    poll_s = (
        config.get("router", {}).get("graceful_terminate_poll_seconds")
        if isinstance(config.get("router"), dict)
        else None
    )
    if not isinstance(poll_s, (int, float)) or poll_s <= 0:
        poll_s = GRACEFUL_TERMINATE_POLL_SECONDS

    def _emit_terminate_progress(stage, message):
        emit_event("swarm_terminate_progress", {
            "request_id": request_id,
            "swarm_id": str(swarm_id),
            "job_id": job_id,
            "provider_backend": provider_backend,
            "stage": str(stage),
            "message": str(message),
            "timestamp": time.time(),
        })

    force_now = False
    if isinstance(terminate_overrides, dict):
        force_now = bool(terminate_overrides.get("force", False))
    with SCHEDULER_LOCK:
        if str(swarm_id) in FORCE_TERMINATION_REQUESTED:
            force_now = True

    # If no nodes are currently active, stale outstanding counters should not
    # block shutdown. Clear them once at terminate start.
    node_count = int(swarm.get("node_count") or 0)
    with SCHEDULER_LOCK:
        all_idle_now = all(
            not bool(NODE_THREAD_ACTIVE.get(_node_key(swarm_id, node_id), False))
            for node_id in range(node_count)
        )
        if all_idle_now:
            for node_id in range(node_count):
                NODE_OUTSTANDING[_node_key(swarm_id, node_id)] = 0

    provider_backend = str(swarm.get("provider_backend") or "").strip().lower()
    if not provider_backend:
        provider_backend = str(swarm.get("backend") or "").strip().lower()
    if not provider_backend:
        provider_ref = str(swarm.get("provider") or "").strip().lower()
        if provider_ref in PROVIDERS:
            resolved = next(
                (item for item in PROVIDER_SPECS if str(item.get("provider_ref", "")).strip().lower() == provider_ref),
                None,
            )
            if isinstance(resolved, dict):
                provider_backend = str(resolved.get("backend") or "").strip().lower()
    if provider_backend and provider_backend not in {str(item.get("backend") or "").strip().lower() for item in PROVIDER_SPECS}:
        # Some persisted states may store provider_id here; resolve to backend.
        spec = next(
            (item for item in PROVIDER_SPECS if str(item.get("id", "")).strip().lower() == provider_backend),
            None,
        )
        if isinstance(spec, dict):
            provider_backend = str(spec.get("backend") or provider_backend).strip().lower()

    # Local swarms should not block for the full global graceful timeout when
    # no tasks are actually in flight. Give local a shorter grace window.
    if provider_backend == "local":
        local_timeout_s = (
            config.get("router", {}).get("local_graceful_terminate_timeout_seconds")
            if isinstance(config.get("router"), dict)
            else None
        )
        if not isinstance(local_timeout_s, (int, float)) or local_timeout_s <= 0:
            local_timeout_s = 20
        timeout_s = min(float(timeout_s), float(local_timeout_s))
    elif provider_backend == "aws":
        # Cloud instance termination can be expensive when delayed by stale
        # node-active state; use a shorter grace window before force terminate.
        aws_timeout_s = (
            config.get("router", {}).get("aws_graceful_terminate_timeout_seconds")
            if isinstance(config.get("router"), dict)
            else None
        )
        if not isinstance(aws_timeout_s, (int, float)) or aws_timeout_s <= 0:
            aws_timeout_s = 45
        timeout_s = min(float(timeout_s), float(aws_timeout_s))

    deadline = time.time() + float(timeout_s)
    timed_out = False
    _emit_terminate_progress("graceful_wait", f"Waiting for swarm quiescence (up to {int(float(timeout_s))}s)")
    if not force_now:
        next_progress_at = time.time() + 5.0
        while time.time() < deadline:
            with SCHEDULER_LOCK:
                if str(swarm_id) in FORCE_TERMINATION_REQUESTED:
                    force_now = True
            if force_now:
                _emit_terminate_progress("force_requested", "Force termination requested")
                break
            if _is_swarm_quiescent_for_termination(swarm_id):
                _emit_terminate_progress("quiescent", "Swarm is quiescent; proceeding with provider termination")
                break
            now = time.time()
            if now >= next_progress_at:
                remaining = max(0, int(deadline - now))
                _emit_terminate_progress(
                    "graceful_wait",
                    f"Still waiting for quiescence ({remaining}s remaining before force terminate)",
                )
                next_progress_at = now + 5.0
            time.sleep(float(poll_s))
        else:
            timed_out = True
            _emit_terminate_progress("graceful_timeout", "Graceful wait timed out; forcing provider termination")

    terminate_params = swarm.get("provider_params") if isinstance(swarm, dict) else None
    if not isinstance(terminate_params, dict):
        terminate_params = {}
    if isinstance(terminate_overrides, dict):
        terminate_params = {**terminate_params, **terminate_overrides}
    terminate_params = {
        **terminate_params,
        "_progress_cb": _emit_terminate_progress,
    }
    _maybe_export_workspace_archive(config, provider, request_id, swarm_id, job_id, terminate_params)

    try:
        _emit_terminate_progress("provider_terminate", "Sending terminate request to provider")
        provider.terminate(job_id, terminate_params=terminate_params)
        _emit_terminate_progress("provider_terminate", "Provider terminate request completed")
    except Exception as e:
        swarm = SWARMS.get(str(swarm_id))
        if swarm and swarm.get("status") == "terminating":
            swarm["status"] = "running"
            swarm.pop("terminating_since", None)
            save_state()
        with SCHEDULER_LOCK:
            TERMINATION_IN_PROGRESS.discard(str(swarm_id))
        emit_event("command_rejected", {
            "request_id": request_id,
            "reason": str(e)
        })
        _emit_terminate_progress("failed", f"Termination failed: {e}")
        return

    _finalize_swarm_termination(provider, request_id, swarm_id, job_id)
    _emit_terminate_progress("completed", "Swarm termination complete")
    if timed_out:
        emit_event("debug", {
            "source": "router",
            "message": f"graceful terminate timed out for swarm {swarm_id}; forced termination"
        })
    with SCHEDULER_LOCK:
        FORCE_TERMINATION_REQUESTED.discard(str(swarm_id))
        TERMINATION_IN_PROGRESS.discard(str(swarm_id))


def _dispatch_inter_swarm_queue(config):
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
            if not target_swarm or target_swarm.get("status") in ("terminated", "terminating"):
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
                save_state()
                continue

            target_provider = _provider_for_swarm(target_swarm_id)
            if not target_provider:
                with SCHEDULER_LOCK:
                    INTER_SWARM_QUEUE[target_swarm_id].popleft()
                    if not INTER_SWARM_QUEUE[target_swarm_id]:
                        INTER_SWARM_QUEUE.pop(target_swarm_id, None)
                emit_event("inter_swarm_dropped", {
                    "queue_id": item.get("queue_id"),
                    "source_swarm_id": item.get("source_swarm_id"),
                    "target_swarm_id": target_swarm_id,
                    "reason": "provider unavailable",
                })
                _emit_queue_updated()
                save_state()
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
                    save_state()
                    continue

                content = item.get("content")
                request_id = item.get("request_id")
                for node_id in targets:
                    threading.Thread(
                        target=perform_injection,
                        args=(config, target_provider, request_id, str(target_swarm_id), str(job_id), int(node_id), content),
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
                save_state()
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
                target_provider,
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
            save_state()


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
    cluster_cfg = config.get("cluster", {})
    slurm_cfg = cluster_cfg.get("slurm", {}) if isinstance(cluster_cfg, dict) else {}
    login_host = slurm_cfg.get("login_host")
    if not isinstance(login_host, str) or not login_host.strip():
        login_host = slurm_cfg.get("login_alias")
    if not isinstance(login_host, str) or not login_host.strip():
        raise RuntimeError("Slurm login_host not configured in cluster.slurm")
    login_host = login_host.strip()
    workspace_root = slurm_cfg.get("workspace_root") or cluster_cfg.get("workspace_root")
    cluster_subdir = slurm_cfg.get("cluster_subdir") or cluster_cfg.get("cluster_subdir")

    outbox_dir = f"{workspace_root}/{cluster_subdir}/mailbox/outbox"

    remote_cmd = (
        f"python3 {workspace_root}/{cluster_subdir}/agent/outbox_follower.py "
        f"{outbox_dir}"
    )

    return subprocess.Popen(
        ["ssh", login_host, remote_cmd],
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
    event_type = event.get("type")
    job_id = str(event.get("job_id"))
    node_id = event.get("node_id")
    injection_id = event.get("injection_id")
    swarm_id = JOB_TO_SWARM.get(job_id)

    # Ignore events from jobs not tracked by this router instance
    if not swarm_id:
        return None

    if event_type == "complete":
        # Worker completion can arrive without a final idle/task_complete rpc.
        # Treat it as idle so graceful termination does not wait on stale state.
        return ("thread_status", {
            "swarm_id": swarm_id,
            "job_id": job_id,
            "node_id": node_id,
            "injection_id": injection_id,
            "status": {"type": "idle"},
        })

    if event_type == "worker_error":
        message = event.get("error")
        call_id = event.get("call_id")
        if (
            isinstance(message, str)
            and message.startswith("synthetic_exec_unsupported:")
            and call_id
        ):
            _prune_pending_approval_for_call(job_id, node_id, call_id, "worker_error_synthetic_exec_unsupported")
        return (
            "agent_error",
            {
                "swarm_id": swarm_id,
                "job_id": job_id,
                "node_id": node_id,
                "injection_id": injection_id,
                "message": message,
                "raw": event,
            },
        )

    if event_type == "session_trace":
        entry = event.get("entry")
        if not isinstance(entry, dict):
            return None

        payload = entry.get("payload")
        if not isinstance(payload, dict):
            return None

        entry_type = entry.get("type")
        base = {
            "swarm_id": swarm_id,
            "job_id": job_id,
            "node_id": node_id,
            "injection_id": injection_id,
        }

        def _emit_session_approval(call_id, command, reason, cwd, proposed_execpolicy_amendment, available_decisions, approval_method, raw_payload):
            if not isinstance(call_id, str) or not call_id.strip():
                return None
            key = _approval_key(job_id, call_id, node_id)
            existing = PENDING_APPROVALS.get(key)
            if existing:
                # If we first synthesized a generic implicit file-change approval
                # from item/started and later observe the richer session trace for
                # the same apply_patch call, upgrade the existing approval route
                # so notification-only replies use the apply-patch decision dialect.
                if (
                    approval_method == "session/custom_tool_call/apply_patch"
                    and existing.get("approval_method") == "item/fileChange/implicitFromStarted"
                    and not bool(existing.get("has_native_approval"))
                ):
                    existing["approval_method"] = approval_method
                    existing["command"] = _choose_richer_approval_command(existing.get("command"), command)
                    existing["available_decisions"] = list(available_decisions or ["approved", "abort"])
                    existing["available_decisions_native"] = list(available_decisions or ["approved", "abort"])
                    existing["available_decisions_item"] = list(available_decisions or ["approved", "abort"])
                    existing["updated_at_ts"] = time.time()
                    approvals_version = _bump_approvals_version()
                    approval_trace(
                        "required_upgraded_from_session_trace",
                        job_id=str(job_id),
                        swarm_id=str(swarm_id),
                        node_id=_normalize_node_id(node_id),
                        call_id=str(call_id),
                        approval_id=existing.get("approval_id"),
                        approvals_version=approvals_version,
                        injection_id=injection_id,
                        turn_id=event.get("turn_id"),
                        approval_method=approval_method,
                    )
                return None
            if _is_recently_resolved_approval(job_id, call_id, node_id):
                return None

            approval_id = str(uuid.uuid4())
            PENDING_APPROVALS[key] = {
                "approval_id": approval_id,
                "approval_status": "pending",
                "swarm_id": swarm_id,
                "node_id": node_id,
                "injection_id": injection_id,
                "turn_id": event.get("turn_id"),
                "rpc_id": None,
                "request_id_hint": None,
                "approval_method": approval_method,
                "command": command,
                "reason": reason,
                "cwd": cwd,
                "proposed_execpolicy_amendment": proposed_execpolicy_amendment,
                "available_decisions": list(available_decisions or ["accept", "cancel"]),
                "available_decisions_native": [],
                "available_decisions_item": list(available_decisions or ["accept", "cancel"]),
                "synthetic_request": True,
                "has_native_approval": False,
                "created_at_ts": time.time(),
                "updated_at_ts": time.time(),
            }
            approvals_version = _bump_approvals_version()
            approval_trace(
                "required_received_session_trace",
                job_id=str(job_id),
                swarm_id=str(swarm_id),
                node_id=_normalize_node_id(node_id),
                call_id=str(call_id),
                approval_id=approval_id,
                approvals_version=approvals_version,
                injection_id=injection_id,
                turn_id=event.get("turn_id"),
                approval_method=approval_method,
            )
            return (
                "exec_approval_required",
                {
                    **base,
                    "approval_id": approval_id,
                    "approvals_version": approvals_version,
                    "turn_id": event.get("turn_id"),
                    "call_id": call_id,
                    "command": command,
                    "reason": reason,
                    "cwd": cwd,
                    "proposed_execpolicy_amendment": proposed_execpolicy_amendment,
                    "available_decisions": list(available_decisions or ["accept", "cancel"]),
                    "raw": raw_payload,
                },
            )

        if entry_type == "response_item" and payload.get("type") == "reasoning":
            content = payload.get("content")
            summary = payload.get("summary")
            encrypted_content = payload.get("encrypted_content")
            has_plaintext = bool(content) or bool(summary)
            if not has_plaintext and isinstance(encrypted_content, str) and encrypted_content:
                return (
                    "reasoning",
                    {
                        **base,
                        "content": "Reasoning captured by Codex is encrypted in the current app-server payload and is not available as plaintext.",
                        "raw": entry,
                    },
                )

        if entry_type == "response_item" and payload.get("type") == "function_call" and payload.get("name") == "exec_command":
            arguments = payload.get("arguments")
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except Exception:
                    arguments = None
            if not isinstance(arguments, dict):
                return None
            if arguments.get("sandbox_permissions") != "require_escalated":
                return None

            prefix_rule = arguments.get("prefix_rule")
            available_decisions = ["accept", "cancel"]
            if isinstance(prefix_rule, list) and prefix_rule:
                available_decisions = [
                    "accept",
                    {
                        "acceptWithExecpolicyAmendment": {
                            "execpolicy_amendment": prefix_rule,
                        }
                    },
                    "cancel",
                ]
            return _emit_session_approval(
                payload.get("call_id"),
                arguments.get("cmd") or arguments.get("command") or "Execute command",
                arguments.get("justification") or "Approve command execution",
                arguments.get("workdir") or arguments.get("cwd"),
                prefix_rule if isinstance(prefix_rule, list) and prefix_rule else None,
                available_decisions,
                "session/function_call/exec_command",
                entry,
            )

        if entry_type == "response_item" and payload.get("type") == "custom_tool_call" and payload.get("name") == "apply_patch":
            parsed_command = _parse_apply_patch_command_from_input(payload.get("input"))
            return _emit_session_approval(
                payload.get("call_id"),
                parsed_command,
                "Approve file changes",
                None,
                None,
                ["approved", "abort"],
                "session/custom_tool_call/apply_patch",
                entry,
            )

        return None

    if event_type != "codex_rpc":
        return None

    payload = event.get("payload", {})
    method = payload.get("method")

    base = {
        "swarm_id": swarm_id,
        "job_id": job_id,
        "node_id": node_id,
        "injection_id": injection_id
    }

    def _collect_text_parts(value):
        if isinstance(value, str):
            return [value]
        if isinstance(value, list):
            parts = []
            for item in value:
                parts.extend(_collect_text_parts(item))
            return parts
        if isinstance(value, dict):
            parts = []
            for key in ("text", "delta", "content", "raw_content", "summary", "summary_text"):
                if key in value:
                    parts.extend(_collect_text_parts(value.get(key)))
            return parts
        return []

    def _extract_reasoning_text(item):
        if not isinstance(item, dict):
            return ""
        chunks = []
        for key in ("summary_text", "summary", "raw_content", "content", "text"):
            chunks.extend(_collect_text_parts(item.get(key)))
        text = "".join(str(chunk) for chunk in chunks if isinstance(chunk, str))
        return text.strip()

    if method == "turn/started":
        return ("turn_started", base)

    if method == "turn/completed":
        return ("turn_complete", base)

    if method == "codex/event/agent_message_content_delta":
        delta = payload["params"]["msg"].get("delta")
        return ("assistant_delta", {**base, "content": delta})

    if method == "codex/event/agent_message":
        msg_obj = payload.get("params", {}).get("msg", {})
        msg = msg_obj.get("message")
        return ("assistant", {
            **base,
            "content": msg,
            "final_answer": str(msg_obj.get("phase") or "").lower() == "final_answer",
        })

    if method in ("codex/event/item_started", "item/started", "codex/event/item_completed", "item/completed"):
        params = payload.get("params", {})
        item = params.get("item")
        if item is None:
            item = params.get("msg", {}).get("item")

        if isinstance(item, dict):
            item_type_raw = item.get("type")
            item_type = re.sub(r"[^a-z]", "", str(item_type_raw).lower())
            if item_type == "reasoning":
                reasoning_text = _extract_reasoning_text(item)
                if reasoning_text:
                    return ("reasoning", {
                        **base,
                        "content": reasoning_text,
                        "raw": payload,
                    })
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
                    return ("assistant", {
                        **base,
                        "content": content,
                        "final_answer": str(item.get("phase") or "").lower() == "final_answer",
                    })
                return ("turn_complete", base)

    if method == "codex/event/token_count":
        info = payload.get("params", {}).get("msg", {}).get("info")
        if isinstance(info, dict):
            usage_payload = _normalize_usage_payload(
                base,
                info.get("total_token_usage"),
                info.get("last_token_usage"),
                info.get("model_context_window"),
                method,
            )
            if usage_payload:
                usage_key = f"{job_id}:{node_id}:{injection_id}"
                last = LAST_USAGE.get(usage_key)
                if last == usage_payload["total_tokens"]:
                    return None
                LAST_USAGE[usage_key] = usage_payload["total_tokens"]
                return ("usage", usage_payload)

    if method == "thread/tokenUsage/updated":
        usage = payload.get("params", {}).get("tokenUsage", {})
        total_usage = usage.get("total", {})
        last_usage = usage.get("last", {})
        usage_payload = _normalize_usage_payload(
            base,
            {
                "total_tokens": total_usage.get("totalTokens"),
                "input_tokens": total_usage.get("inputTokens"),
                "cached_input_tokens": total_usage.get("cachedInputTokens"),
                "output_tokens": total_usage.get("outputTokens"),
                "reasoning_output_tokens": total_usage.get("reasoningOutputTokens"),
            },
            {
                "total_tokens": last_usage.get("totalTokens"),
                "input_tokens": last_usage.get("inputTokens"),
                "cached_input_tokens": last_usage.get("cachedInputTokens"),
                "output_tokens": last_usage.get("outputTokens"),
                "reasoning_output_tokens": last_usage.get("reasoningOutputTokens"),
            },
            usage.get("modelContextWindow"),
            method,
        )
        if usage_payload:
            usage_key = f"{job_id}:{node_id}:{injection_id}"
            last = LAST_USAGE.get(usage_key)
            if last == usage_payload["total_tokens"]:
                return None
            LAST_USAGE[usage_key] = usage_payload["total_tokens"]
            return ("usage", usage_payload)

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
    if method in (
        "codex/event/exec_approval_request",
        "item/commandExecution/requestApproval",
        "codex/event/apply_patch_approval_request",
        "item/fileChange/requestApproval",
        "execCommandApproval",
        "applyPatchApproval",
    ):
        params = payload.get("params", {})
        msg = params.get("msg")
        event_turn_id = None

        if msg:
            if method == "codex/event/apply_patch_approval_request":
                # Shape A2: codex/event/apply_patch_approval_request
                call_id = msg.get("call_id")
                changes = msg.get("changes")
                command = {"changes": changes} if isinstance(changes, dict) else "Apply file changes"
                reason = msg.get("reason") or "Approve file changes"
                cwd = msg.get("cwd")
                proposed_execpolicy_amendment = None
                # Native apply-patch approvals use approved/abort-style decisions.
                available_decisions = msg.get("available_decisions") or ["approved", "abort"]
                event_turn_id = msg.get("turn_id")
            else:
                # Shape A: codex/event/exec_approval_request
                call_id = msg.get("call_id")
                command = msg.get("command")
                reason = msg.get("reason")
                cwd = msg.get("cwd")
                proposed_execpolicy_amendment = msg.get("proposed_execpolicy_amendment")
                available_decisions = msg.get("available_decisions")
                event_turn_id = msg.get("turn_id")
        else:
            if method == "item/fileChange/requestApproval":
                # Shape B2: item/fileChange/requestApproval
                call_id = params.get("itemId")
                changes = params.get("changes")
                command = {"changes": changes} if isinstance(changes, (list, dict)) else "Apply file changes"
                reason = params.get("reason") or "Approve file changes"
                cwd = params.get("cwd")
                proposed_execpolicy_amendment = None
                available_decisions = (
                    params.get("availableDecisions")
                    or params.get("available_decisions")
                    or ["accept", "cancel"]
                )
                event_turn_id = params.get("turnId")
            elif method == "item/commandExecution/requestApproval":
                # Shape B: item/commandExecution/requestApproval
                call_id = params.get("itemId")
                command = params.get("command")
                reason = params.get("reason")
                cwd = params.get("cwd")
                proposed_execpolicy_amendment = params.get("proposedExecpolicyAmendment") or params.get("proposed_execpolicy_amendment")
                available_decisions = params.get("availableDecisions") or params.get("available_decisions")
                event_turn_id = params.get("turnId")
            elif method == "applyPatchApproval":
                # Shape C1: direct review-decision patch approval RPC
                call_id = params.get("callId")
                changes = params.get("fileChanges")
                command = {"changes": changes} if isinstance(changes, dict) else "Apply file changes"
                reason = params.get("reason") or "Approve file changes"
                cwd = params.get("grantRoot")
                proposed_execpolicy_amendment = None
                available_decisions = ["approved", "approved_for_session", "denied", "abort"]
                event_turn_id = None
            else:
                # Shape C2: direct review-decision command approval RPC
                call_id = params.get("callId")
                command = params.get("command")
                reason = params.get("reason")
                cwd = params.get("cwd")
                proposed_execpolicy_amendment = None
                available_decisions = ["approved", "approved_for_session", "denied", "abort"]
                event_turn_id = None

        raw_rpc_id = payload.get("id")
        rpc_id = raw_rpc_id if _is_jsonrpc_id(raw_rpc_id) else None
        payload_id_hint = rpc_id
        request_id_hint = None

        if call_id:
            key = _approval_key(job_id, call_id, node_id)
            existing = PENDING_APPROVALS.get(key, {})
            resolved_recently = _is_recently_resolved_approval(job_id, call_id, node_id)

            if resolved_recently:
                recent = _get_recent_approval_result(job_id, call_id, node_id)
                if recent:
                    approval_provider = _provider_for_swarm(swarm_id)
                    replay_ok = False
                    if approval_provider is not None:
                        replay_ok = _send_duplicate_approval_replay(
                            approval_provider,
                            job_id,
                            node_id,
                            str(call_id),
                            rpc_id if rpc_id is not None else existing.get("rpc_id"),
                            request_id_hint if request_id_hint is not None else existing.get("request_id_hint"),
                            bool(recent.get("approved")),
                            recent.get("decision"),
                            approval_method=method if method else existing.get("approval_method"),
                            has_native_approval=bool(existing.get("has_native_approval")),
                            turn_id=event_turn_id if event_turn_id else existing.get("turn_id"),
                        )
                    approval_trace(
                        "required_ignored_recently_resolved",
                        job_id=str(job_id),
                        swarm_id=str(swarm_id),
                        node_id=_normalize_node_id(node_id),
                        call_id=str(call_id),
                        replayed=bool(replay_ok),
                    )
                else:
                    approval_trace(
                        "required_ignored_recently_resolved",
                        job_id=str(job_id),
                        swarm_id=str(swarm_id),
                        node_id=_normalize_node_id(node_id),
                        call_id=str(call_id),
                        replayed=False,
                    )
                return None

            # Keep the strongest request shape when both legacy and request-style
            # approval events are emitted for the same call_id.
            existing_rpc_id = existing.get("rpc_id")
            native_approval_method = method in (
                "codex/event/exec_approval_request",
                "codex/event/apply_patch_approval_request",
            )
            # If the incoming approval event has a top-level JSON-RPC id, treat
            # it as request-correlated and respond with that same id.
            if rpc_id is not None:
                merged_rpc_id = rpc_id
            else:
                merged_rpc_id = existing_rpc_id

            existing_available = existing.get("available_decisions") or []
            existing_native_available = existing.get("available_decisions_native") or []
            existing_item_available = existing.get("available_decisions_item") or []
            new_available = available_decisions or []
            new_native_available = list(new_available) if native_approval_method else []
            new_item_available = list(new_available) if not native_approval_method else []

            def _has_accept_decisions(decisions):
                return any(
                    (isinstance(d, str) and d in ("accept", "cancel")) or
                    (isinstance(d, dict) and "acceptWithExecpolicyAmendment" in d)
                    for d in (decisions or [])
                )

            def _has_policy_amendment_option(decisions):
                return any(
                    (isinstance(d, dict) and (
                        "acceptWithExecpolicyAmendment" in d
                        or "approved_execpolicy_amendment" in d
                    ))
                    for d in (decisions or [])
                )

            def _decision_key(decision):
                if isinstance(decision, str):
                    return f"s:{decision}"
                if isinstance(decision, dict):
                    try:
                        return "j:" + json.dumps(decision, sort_keys=True)
                    except Exception:
                        return "o:" + str(decision)
                return "x:" + str(decision)

            if not existing_available:
                merged_available = list(new_available)
            elif not new_available:
                merged_available = list(existing_available)
            else:
                # Merge decisions from both event shapes so UI options do not
                # oscillate (e.g., "Approve + Remember" disappearing mid-turn).
                merged_available = []
                seen_keys = set()
                for decision in list(existing_available) + list(new_available):
                    key_token = _decision_key(decision)
                    if key_token in seen_keys:
                        continue
                    seen_keys.add(key_token)
                    merged_available.append(decision)

                # Preserve policy-amendment option once seen for this call_id.
                if (
                    _has_policy_amendment_option(existing_available)
                    and not _has_policy_amendment_option(merged_available)
                ):
                    merged_available = list(existing_available)
                elif (
                    _has_accept_decisions(new_available)
                    and not _has_accept_decisions(merged_available)
                ):
                    merged_available = list(new_available)

            if not existing_native_available:
                merged_native_available = list(new_native_available)
            elif not new_native_available:
                merged_native_available = list(existing_native_available)
            else:
                merged_native_available = list(existing_native_available)
                for decision in new_native_available:
                    token = _decision_key(decision)
                    if all(_decision_key(existing_decision) != token for existing_decision in merged_native_available):
                        merged_native_available.append(decision)

            if not existing_item_available:
                merged_item_available = list(new_item_available)
            elif not new_item_available:
                merged_item_available = list(existing_item_available)
            else:
                merged_item_available = list(existing_item_available)
                for decision in new_item_available:
                    token = _decision_key(decision)
                    if all(_decision_key(existing_decision) != token for existing_decision in merged_item_available):
                        merged_item_available.append(decision)

            # Some runtimes emit duplicate approval events where one shape has
            # no turn_id and/or stale injection_id. Preserve existing
            # correlation fields unless the new event has stronger identifiers.
            existing_injection_id = existing.get("injection_id")
            existing_turn_id = existing.get("turn_id")
            merged_injection_id = injection_id
            if existing_injection_id and (not event_turn_id or not injection_id):
                merged_injection_id = existing_injection_id
            merged_turn_id = event_turn_id if event_turn_id else existing_turn_id
            approval_id = existing.get("approval_id") or str(uuid.uuid4())

            has_native_approval = bool(existing.get("has_native_approval")) or native_approval_method
            existing_approval_method = existing.get("approval_method")
            existing_native_method = existing_approval_method in (
                "codex/event/exec_approval_request",
                "codex/event/apply_patch_approval_request",
            )
            # Keep the approval method anchored to native codex/event/* once seen.
            # Duplicate item/* events for the same call_id must not downgrade routing
            # to notification mode when an RPC response is required.
            if has_native_approval and existing_native_method:
                merged_approval_method = existing_approval_method
            else:
                merged_approval_method = method
            requested_synthetic = bool(
                params.get("synthetic_request") is True
                or (isinstance(msg, dict) and msg.get("synthetic_request") is True)
            )
            merged_synthetic_request = bool(existing.get("synthetic_request") or requested_synthetic)
            if has_native_approval:
                # Native approval RPC observed for this call_id: never treat it
                # as synthetic or we may skip the required RPC response.
                merged_synthetic_request = False
                # Keep merged decisions across duplicate shapes for the same call.
                # Some runtimes emit mixed dialects (item/* => accept/cancel and
                # codex/event/* => approved/abort) for one call_id. Keeping the
                # union allows downstream normalization to pick a compatible token
                # per response channel.

            # Worker-side synthetic approvals are only placeholders to surface an
            # escalation before Codex app-server emits its real correlated request.
            # Once commandExecution approval arrives with a top-level JSON-RPC id,
            # Codex expects to execute the command on the worker host and receive
            # its own exec_command lifecycle. Do not steal execution into router.
            if (
                method == "item/commandExecution/requestApproval"
                and merged_rpc_id is not None
            ):
                merged_synthetic_request = False

            existing_status = str(existing.get("approval_status") or "")
            if existing_status in ("approved_pending_ack", "denied_pending_ack"):
                # Preserve the submitted state across late duplicate/native
                # approval shapes for the same call_id. Reopening them as
                # pending causes approval dialogs to flash and can require an
                # unnecessary second click.
                merged_status = existing_status
            elif existing_status in ("started", "resolved"):
                merged_status = existing_status
            else:
                merged_status = "pending"

            existing_request_id_hint = existing.get("request_id_hint")
            # Keep hint stable once set; do not overwrite from duplicate item/*
            # events because those ids are frequently non-correlating.
            if existing_request_id_hint is not None:
                merged_request_id_hint = existing_request_id_hint
            else:
                merged_request_id_hint = request_id_hint

            PENDING_APPROVALS[key] = {
                "approval_id": approval_id,
                "approval_status": merged_status,
                "swarm_id": swarm_id,
                "node_id": node_id,
                "injection_id": merged_injection_id,
                "turn_id": merged_turn_id,
                "rpc_id": merged_rpc_id,
                # For non-native item/* approvals, preserve top-level payload id
                # as a compatibility hint even when we stay in notification mode.
                # Some runtimes accept/require rpc_response against that id.
                "request_id_hint": merged_request_id_hint,
                "approval_method": merged_approval_method,
                "command": command,
                "reason": reason,
                "cwd": cwd,
                "proposed_execpolicy_amendment": proposed_execpolicy_amendment,
                "available_decisions": merged_available,
                "available_decisions_native": merged_native_available,
                "available_decisions_item": merged_item_available,
                "synthetic_request": merged_synthetic_request,
                "has_native_approval": has_native_approval,
                "created_at_ts": time.time(),
                "updated_at_ts": time.time(),
            }
            approvals_version = _bump_approvals_version()
            approval_trace(
                "required_received",
                job_id=str(job_id),
                swarm_id=str(swarm_id),
                node_id=_normalize_node_id(node_id),
                call_id=str(call_id),
                approval_id=approval_id,
                approvals_version=approvals_version,
                injection_id=injection_id,
                turn_id=event_turn_id,
                approval_method=method,
                rpc_id=merged_rpc_id,
                request_id_hint=request_id_hint,
                available_decisions_count=len(merged_available) if isinstance(merged_available, list) else None,
            )

            # If the user already submitted a decision for this call_id, replay
            # that exact approval onto any later native/request-correlated
            # approval event without requiring a second click.
            pending_decision = PENDING_APPROVAL_DECISIONS.get(key)
            if (
                pending_decision
                and existing_status in ("approved_pending_ack", "denied_pending_ack")
            ):
                try:
                    approval_provider = _provider_for_swarm(swarm_id)
                    if approval_provider is None:
                        approval_provider = ACTIVE_PROVIDER
                    approved_flag = bool(pending_decision.get("approved"))
                    raw_decision = pending_decision.get("decision")
                    rpc_decision = _normalize_decision_for_available(
                        raw_decision,
                        approved_flag,
                        merged_available,
                        native_approval_method or has_native_approval,
                        proposed_execpolicy_amendment,
                    )
                    rpc_decision = _coerce_to_advertised_decision(
                        rpc_decision,
                        merged_available,
                        approved_flag,
                        native_approval_method or has_native_approval,
                    )
                    notify_decision = rpc_decision
                    notify_params = {
                        "call_id": call_id,
                        "callId": call_id,
                        "approved": approved_flag,
                        "decision": notify_decision,
                        **(notify_decision if isinstance(notify_decision, dict) else {}),
                    }
                    if event_turn_id:
                        notify_params["id"] = event_turn_id
                        notify_params["turn_id"] = event_turn_id
                        notify_params["turnId"] = event_turn_id
                    compat_notify_payload = {
                        "method": "exec/approvalResponse",
                        "params": notify_params,
                    }
                    primary_control_payload = (
                        {
                            "type": "rpc_response",
                            "rpc_id": merged_rpc_id,
                            "result": {
                                "decision": rpc_decision,
                            },
                        }
                        if merged_rpc_id is not None
                        else compat_notify_payload
                    )
                    compat_rpc_payload = None
                    if approval_provider is not None:
                        target_node_id = existing.get("node_id", node_id)
                        _send_approval_decision(
                            approval_provider,
                            job_id,
                            target_node_id,
                            primary_control_payload,
                            compat_rpc_payload,
                            compat_notify_payload,
                        )
                        approval_trace(
                            "required_autoreplay_sent",
                            job_id=str(job_id),
                            swarm_id=str(swarm_id),
                            node_id=_normalize_node_id(target_node_id),
                            call_id=str(call_id),
                            approval_id=approval_id,
                            rpc_id=merged_rpc_id,
                            fallback_notify=True,
                            native_request=bool(rpc_id is not None),
                            stored_decision=raw_decision,
                            replay_decision=rpc_decision,
                            available_decisions=merged_available,
                        )
                        pending_decision["control_payload"] = primary_control_payload
                        pending_decision["compat_rpc_payload"] = compat_rpc_payload
                        pending_decision["compat_notify_payload"] = compat_notify_payload
                        pending_decision["job_id"] = str(job_id)
                        pending_decision["node_id"] = target_node_id
                        pending_decision["send_attempts"] = int(pending_decision.get("send_attempts", 0) or 0) + 1
                        pending_decision["last_sent_ts"] = time.time()
                        PENDING_APPROVAL_DECISIONS[key] = pending_decision
                        PENDING_APPROVALS[key]["approval_status"] = (
                            "approved_pending_ack" if approved_flag else "denied_pending_ack"
                        )
                        PENDING_APPROVALS[key]["updated_at_ts"] = time.time()
                        _bump_approvals_version()
                except Exception as e:
                    approval_trace(
                        "required_autoreplay_error",
                        job_id=str(job_id),
                        swarm_id=str(swarm_id),
                        node_id=_normalize_node_id(node_id),
                        call_id=str(call_id),
                        approval_id=approval_id,
                        rpc_id=merged_rpc_id,
                        error=str(e),
                    )

        return (
            "exec_approval_required",
            {
                **base,
                "approval_id": approval_id if call_id else None,
                "approvals_version": APPROVALS_VERSION,
                "injection_id": base.get("injection_id"),
                "turn_id": event_turn_id,
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
        call_id = msg.get("call_id")
        if call_id:
            _prune_pending_approval_for_call(job_id, node_id, call_id, "command_begin")
        return (
            "command_started",
            {
                **base,
                "turn_id": msg.get("turn_id"),
                "call_id": msg.get("call_id"),
                "command": msg.get("command"),
                "cwd": msg.get("cwd"),
                "raw": payload
            }
        )

    if method == "codex/event/exec_command_end":
        msg = payload.get("params", {}).get("msg", {})
        call_id = msg.get("call_id")
        if call_id:
            _prune_pending_approval_for_call(job_id, node_id, call_id, "command_end")
        return (
            "command_completed",
            {
                **base,
                "turn_id": msg.get("turn_id"),
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

    if method == "codex/event/patch_apply_begin":
        msg = payload.get("params", {}).get("msg", {})
        call_id = msg.get("call_id")
        if call_id:
            _prune_pending_approval_for_call(job_id, node_id, call_id, "patch_apply_begin")
        return (
            "filechange_started",
            {
                **base,
                "turn_id": msg.get("turn_id"),
                "call_id": call_id,
                "raw": payload,
            },
        )

    if method == "codex/event/patch_apply_end":
        msg = payload.get("params", {}).get("msg", {})
        call_id = msg.get("call_id")
        if call_id:
            _prune_pending_approval_for_call(job_id, node_id, call_id, "patch_apply_end")
        return (
            "filechange_completed",
            {
                **base,
                "turn_id": msg.get("turn_id"),
                "call_id": call_id,
                "status": msg.get("status"),
                "raw": payload,
            },
        )

    if method in ("item/started", "item/completed", "codex/event/item_started", "codex/event/item_completed"):
        params = payload.get("params", {})
        item = params.get("item")
        if item is None:
            item = params.get("msg", {}).get("item", {})

        item_type = None
        call_id = None
        if isinstance(item, dict):
            item_type = re.sub(r"[^a-z]", "", str(item.get("type") or "").lower())
            call_id = item.get("id")

        if call_id and item_type in ("filechange", "commandexecution"):
            started = method in ("item/started", "codex/event/item_started")
            if item_type == "filechange":
                if started:
                    key = _approval_key(job_id, call_id, node_id)
                    existing = PENDING_APPROVALS.get(key)
                    resolved_recently = _is_recently_resolved_approval(job_id, call_id, node_id)
                    if not existing and not resolved_recently:
                        approval_id = str(uuid.uuid4())
                        command = {"changes": item.get("changes")} if isinstance(item.get("changes"), (list, dict)) else "Apply file changes"
                        PENDING_APPROVALS[key] = {
                            "approval_id": approval_id,
                            "approval_status": "pending",
                            "swarm_id": swarm_id,
                            "node_id": node_id,
                            "injection_id": base.get("injection_id"),
                            "turn_id": params.get("turnId") or params.get("msg", {}).get("turn_id"),
                            "rpc_id": None,
                            "request_id_hint": None,
                            "approval_method": "item/fileChange/implicitFromStarted",
                            "command": command,
                            "reason": "Approve file changes",
                            "cwd": item.get("cwd"),
                            "proposed_execpolicy_amendment": None,
                            "available_decisions": ["accept", "cancel"],
                            "available_decisions_native": [],
                            "available_decisions_item": ["accept", "cancel"],
                            "synthetic_request": False,
                            "has_native_approval": False,
                            "created_at_ts": time.time(),
                            "updated_at_ts": time.time(),
                        }
                        approvals_version = _bump_approvals_version()
                        approval_trace(
                            "required_received_implicit",
                            job_id=str(job_id),
                            swarm_id=str(swarm_id),
                            node_id=_normalize_node_id(node_id),
                            call_id=str(call_id),
                            approval_id=approval_id,
                            approvals_version=approvals_version,
                            injection_id=base.get("injection_id"),
                            turn_id=params.get("turnId") or params.get("msg", {}).get("turn_id"),
                            approval_method="item/fileChange/implicitFromStarted",
                        )
                        return (
                            "exec_approval_required",
                            {
                                **base,
                                "approval_id": approval_id,
                                "approvals_version": approvals_version,
                                "injection_id": base.get("injection_id"),
                                "turn_id": params.get("turnId") or params.get("msg", {}).get("turn_id"),
                                "call_id": call_id,
                                "command": command,
                                "reason": "Approve file changes",
                                "cwd": item.get("cwd"),
                                "proposed_execpolicy_amendment": None,
                                "available_decisions": ["accept", "cancel"],
                                "raw": payload,
                            },
                        )
                source = "filechange_item_started" if started else "filechange_item_completed"
                event_name = "filechange_started" if started else "filechange_completed"
                if not started:
                    _prune_pending_approval_for_call(job_id, node_id, call_id, source)
                return (
                    event_name,
                    {
                        **base,
                        "turn_id": params.get("turnId") or params.get("msg", {}).get("turn_id"),
                        "call_id": call_id,
                        "raw": payload,
                    },
                )

            source = "command_item_started" if started else "command_item_completed"
            event_name = "command_started" if started else "command_completed"
            if not started:
                _prune_pending_approval_for_call(job_id, node_id, call_id, source)
            return (
                event_name,
                {
                    **base,
                    "turn_id": params.get("turnId") or params.get("msg", {}).get("turn_id"),
                    "call_id": call_id,
                    "command": item.get("command"),
                    "cwd": item.get("cwd"),
                    "stdout": item.get("stdout") or item.get("aggregatedOutput"),
                    "stderr": item.get("stderr"),
                    "exit_code": item.get("exitCode"),
                    "duration": item.get("durationMs"),
                    "status": item.get("status"),
                    "raw": payload,
                },
            )

        if call_id and item_type == "dynamictoolcall":
            started = method in ("item/started", "codex/event/item_started")
            if not started:
                _prune_pending_approval_for_call(job_id, node_id, call_id, "dynamic_tool_item_completed")
            return None

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

def run_daemon(config, providers):
    global ACTIVE_PROVIDER
    ACTIVE_PROVIDER = None

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

    # Start followers lazily per provider_ref. Do not prestart followers for
    # configured-but-idle backends/profiles.
    follower_procs = {}
    stdout_buffers = {}
    follower_restart_after = {}
    follower_starting = set()
    follower_enabled = set()

    def start_follower_async(backend, provider):
        if backend in follower_starting:
            return
        follower_starting.add(backend)
        try:
            proc = provider.start_follower()
            if proc:
                follower_procs[backend] = proc
                stdout_buffers[backend] = b""
                follower_restart_after[backend] = time.time() + 1.0
                if DEBUG:
                    print(f"[router DEBUG] follower started: {backend}", flush=True)
        except Exception as e:
            follower_restart_after[backend] = time.time() + 3.0
            print(f"Follower failed to start for {backend}: {e}", flush=True)
        finally:
            follower_starting.discard(backend)

    def enable_follower(backend):
        if not backend:
            return
        if backend not in providers:
            return
        follower_enabled.add(backend)
        if backend in follower_procs or backend in follower_starting:
            return
        threading.Thread(target=start_follower_async, args=(backend, providers[backend]), daemon=True).start()

    # If state recovery restored running swarms, enable followers only for those
    # specific provider refs.
    for swarm in SWARMS.values():
        if not isinstance(swarm, dict):
            continue
        provider_ref = swarm.get("provider")
        provider_backend = swarm.get("provider_backend")
        backend_key = provider_ref if provider_ref in providers else provider_backend
        if isinstance(backend_key, str):
            enable_follower(backend_key)

    debug_event("daemon_started")
    # Resume queued inter-swarm work after router restart.
    _dispatch_inter_swarm_queue(config)

    while True:
        # Remove exited follower processes so EOF pipes do not cause a tight
        # select loop with immediate wakeups.
        for backend, proc in list(follower_procs.items()):
            try:
                exited = proc.poll() is not None
            except Exception:
                exited = True
            if exited:
                follower_procs.pop(backend, None)
                stdout_buffers.pop(backend, None)
                follower_restart_after[backend] = time.time() + 2.0
                if DEBUG:
                    print(f"[router DEBUG] follower exited: {backend}", flush=True)

        now = time.time()
        for backend in list(follower_enabled):
            provider = providers.get(backend)
            if not provider:
                continue
            if backend in follower_procs or backend in follower_starting:
                continue
            retry_at = float(follower_restart_after.get(backend, 0.0) or 0.0)
            if now < retry_at:
                continue
            follower_restart_after[backend] = now + 2.0
            if DEBUG:
                print(f"[router DEBUG] restarting follower: {backend}", flush=True)
            threading.Thread(target=start_follower_async, args=(backend, provider), daemon=True).start()

        streams = []
        fd_to_stream = {}
        for backend, proc in follower_procs.items():
            if not proc:
                continue
            if proc.stdout:
                streams.append(proc.stdout)
                fd_to_stream[proc.stdout.fileno()] = ("stdout", backend, proc.stdout)
            if proc.stderr:
                streams.append(proc.stderr)
                fd_to_stream[proc.stderr.fileno()] = ("stderr", backend, proc.stderr)

        if streams:
            ready, _, _ = select.select(streams, [], [], 0.2)
        else:
            time.sleep(0.2)
            ready = []

        dead_backends = set()
        for stream in ready:
            stream_type, backend, raw_stream = fd_to_stream.get(stream.fileno(), (None, None, None))
            if stream_type is None:
                continue

            chunk = os.read(raw_stream.fileno(), 4096)
            if not chunk:
                dead_backends.add(backend)
                continue

            if stream_type == "stderr":
                line = chunk.decode(errors="ignore").strip()
                if line and DEBUG:
                    print(f"[follower:{backend}:stderr] {line}", flush=True)
                continue

            stdout_buffers[backend] = stdout_buffers.get(backend, b"") + chunk
            while b"\n" in stdout_buffers[backend]:
                line_raw, rest = stdout_buffers[backend].split(b"\n", 1)
                stdout_buffers[backend] = rest
                line = line_raw.decode().strip()
                if not line:
                    continue

                try:
                    event = json.loads(line)
                except Exception:
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
                            _dispatch_inter_swarm_queue(config)
                    if event_name == "turn_complete":
                        _mark_outstanding(data.get("swarm_id"), data.get("node_id"), -1)
                        _dispatch_inter_swarm_queue(config)
                    if event_name == "task_complete":
                        # Some traces emit task_complete without a matching turn_complete;
                        # reconcile outstanding count to avoid idle-queue starvation.
                        _mark_outstanding(data.get("swarm_id"), data.get("node_id"), -1)
                        _dispatch_inter_swarm_queue(config)
                    if event_name == "assistant" and bool(data.get("final_answer")):
                        final_key = (
                            str(data.get("swarm_id")),
                            int(data.get("node_id") if isinstance(data.get("node_id"), int) else -1),
                            str(data.get("injection_id") or ""),
                        )
                        should_reconcile = False
                        with SCHEDULER_LOCK:
                            if final_key not in FINAL_ANSWER_SEEN:
                                FINAL_ANSWER_SEEN.add(final_key)
                                should_reconcile = True
                                node_id = data.get("node_id")
                                if isinstance(node_id, int):
                                    NODE_THREAD_ACTIVE[_node_key(data.get("swarm_id"), node_id)] = False
                        if should_reconcile:
                            _mark_outstanding(data.get("swarm_id"), data.get("node_id"), -1)
                        _dispatch_inter_swarm_queue(config)
                    emit_event(event_name, data)

        for backend in dead_backends:
            follower_procs.pop(backend, None)
            stdout_buffers.pop(backend, None)
            follower_restart_after[backend] = time.time() + 1.0
            if DEBUG:
                print(f"[router DEBUG] follower stream EOF: {backend}", flush=True)

        # Keep approval delivery resilient across transient transport gaps.
        _retry_unacked_approvals()

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
                agents_bundle = payload.get("agents_bundle")
                default_agents_md = _load_default_agents_md()
                provider_id = payload.get("provider")
                provider_params = payload.get("provider_params")
                agents_md_content = _normalize_agents_text(agents_md_content)
                if not isinstance(agents_bundle, dict):
                    agents_bundle = None
                else:
                    bundle_md = agents_bundle.get("agents_md_content")
                    mode = agents_bundle.get("mode")
                    raw_skills = agents_bundle.get("skills_files")
                    bundle_md = _normalize_agents_text(bundle_md)
                    if mode not in ("file", "directory"):
                        mode = "file"
                    skills_files = []
                    if isinstance(raw_skills, list):
                        for item in raw_skills:
                            if not isinstance(item, dict):
                                continue
                            rel_path = item.get("path")
                            content = item.get("content")
                            if (
                                isinstance(rel_path, str)
                                and rel_path.strip()
                                and isinstance(content, str)
                            ):
                                skills_files.append({
                                    "path": rel_path.strip(),
                                    "content": content,
                                })
                    agents_bundle = {
                        "mode": mode,
                        "agents_md_content": bundle_md,
                        "skills_files": skills_files,
                    }
                    if bundle_md is None and not skills_files:
                        agents_bundle = None
                if agents_bundle is not None:
                    agents_bundle["agents_md_content"] = _merge_agents_md(
                        default_agents_md,
                        agents_bundle.get("agents_md_content"),
                    )
                else:
                    agents_md_content = _merge_agents_md(default_agents_md, agents_md_content)
                if not isinstance(provider_params, dict):
                    provider_params = {}

                spec_by_id = {spec.get("id"): spec for spec in PROVIDER_SPECS}
                if not provider_id:
                    provider_id = PROVIDER_SPECS[0]["id"] if PROVIDER_SPECS else None
                provider_spec = spec_by_id.get(provider_id)
                if not provider_spec:
                    emit_event("command_rejected", {
                        "request_id": request_id,
                        "reason": f"unknown provider: {provider_id}"
                    })
                    continue
                provider_backend = provider_spec.get("backend")
                provider_ref = provider_spec.get("provider_ref") or provider_backend
                launch_provider = _provider_for_id(provider_ref)
                if not launch_provider:
                    emit_event("command_rejected", {
                        "request_id": request_id,
                        "reason": f"provider backend unavailable: {provider_backend}"
                    })
                    continue
                # Ensure follower is active for the provider handling this launch.
                enable_follower(provider_ref)
                launch_defaults = provider_spec.get("defaults")
                launch_defaults = launch_defaults if isinstance(launch_defaults, dict) else {}
                effective_launch_params = {**launch_defaults, **provider_params}

                launch_request_id = request_id
                launch_nodes = nodes
                launch_system_prompt = system_prompt
                launch_provider_backend = provider_backend
                launch_provider_id = provider_id
                launch_provider_ref = provider_ref
                launch_effective_params = dict(effective_launch_params)
                launch_agents_md_content = agents_md_content
                launch_agents_bundle = agents_bundle
                launch_provider_obj = launch_provider

                emit_event("swarm_launch_progress", {
                    "request_id": launch_request_id,
                    "provider": launch_provider_backend,
                    "provider_id": launch_provider_id,
                    "stage": "queued",
                    "message": "Launch request queued",
                    "timestamp": time.time(),
                })

                def _launch_progress(stage, message):
                    emit_event("swarm_launch_progress", {
                        "request_id": launch_request_id,
                        "provider": launch_provider_backend,
                        "provider_id": launch_provider_id,
                        "stage": str(stage),
                        "message": str(message),
                        "timestamp": time.time(),
                    })

                def _run_launch():
                    try:
                        job_id = launch_provider_obj.launch(
                            launch_nodes,
                            agents_md_content=launch_agents_md_content,
                            agents_bundle=launch_agents_bundle,
                            launch_params=launch_effective_params,
                            progress_cb=_launch_progress,
                        )
                    except Exception as e:
                        emit_event("command_rejected", {
                            "request_id": launch_request_id,
                            "reason": str(e)
                        })
                        return

                    swarm_id = str(uuid.uuid4())

                    SWARMS[swarm_id] = {
                        "job_id": job_id,
                        "node_count": launch_nodes,
                        "system_prompt": launch_system_prompt,
                        "status": "running",
                        "provider": launch_provider_ref,
                        "provider_backend": launch_provider_backend,
                        "provider_id": launch_provider_id,
                        "provider_params": launch_effective_params,
                    }

                    JOB_TO_SWARM[job_id] = swarm_id
                    with SCHEDULER_LOCK:
                        for node_id in range(launch_nodes):
                            NODE_THREAD_ACTIVE[_node_key(swarm_id, node_id)] = False
                    try:
                        launch_provider_obj.bind_swarm(job_id, swarm_id, SWARMS[swarm_id])
                    except Exception:
                        pass
                    save_state()

                    emit_event("swarm_launched", {
                        "request_id": launch_request_id,
                        "swarm_id": swarm_id,
                        "job_id": job_id,
                        "node_count": launch_nodes,
                        "provider": launch_provider_backend,
                        "provider_id": launch_provider_id,
                    })

                    for node_id in range(launch_nodes):
                        threading.Thread(
                            target=perform_injection,
                            args=(
                                config,
                                launch_provider_obj,
                                launch_request_id,
                                swarm_id,
                                job_id,
                                node_id,
                                launch_system_prompt,
                            ),
                            daemon=True
                        ).start()
                    _dispatch_inter_swarm_queue(config)

                threading.Thread(target=_run_launch, daemon=True).start()
                continue

            elif command == "inject":
                swarm_id = payload.get("swarm_id")
                nodes = payload.get("nodes", "all")
                content = payload.get("content")

                swarm = SWARMS.get(swarm_id)
                if not swarm:
                    continue
                if swarm.get("status") in ("terminating", "terminated"):
                    emit_event("command_rejected", {
                        "request_id": request_id,
                        "reason": "swarm is terminating or terminated"
                    })
                    continue

                job_id = swarm["job_id"]
                node_count = swarm["node_count"]
                swarm_provider = _provider_for_swarm(swarm_id)
                if not swarm_provider:
                    emit_event("command_rejected", {
                        "request_id": request_id,
                        "reason": "provider unavailable for swarm"
                    })
                    continue

                if nodes == "all":
                    targets = range(node_count)
                elif isinstance(nodes, list):
                    targets = nodes
                else:
                    targets = [nodes]

                for node_id in targets:
                    threading.Thread(
                        target=perform_injection,
                        args=(config, swarm_provider, request_id, swarm_id, job_id, node_id, content),
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
                if target_swarm.get("status") in ("terminating", "terminated"):
                    emit_event("command_rejected", {
                        "request_id": request_id,
                        "reason": "target swarm is terminating or terminated"
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
                save_state()
                _dispatch_inter_swarm_queue(config)

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

            elif command == "approvals_list":
                emit_event("approvals_list", {
                    "request_id": request_id,
                    "approvals_version": APPROVALS_VERSION,
                    "approvals": _pending_approvals_snapshot()
                })

            elif command == "providers_list":
                emit_event("providers_list", {
                    "request_id": request_id,
                    "providers": PROVIDER_SPECS,
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
                        swarm_provider = _provider_for_swarm(swarm_id)
                        if not swarm_provider:
                            emit_event("swarm_status", {
                                "request_id": request_id,
                                "swarm_id": swarm_id,
                                "job_id": job_id,
                                "node_count": swarm.get("node_count"),
                                "status": swarm.get("status", "unknown"),
                                "error": "provider unavailable",
                            })
                            return

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

                        state = swarm_provider.get_job_state(job_id)

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
                node_id = payload.get("node_id")
                injection_id = payload.get("injection_id")
                approved = payload.get("approved")
                decision = payload.get("decision")
                approval_trace(
                    "approve_command_received",
                    request_id=request_id,
                    job_id=str(job_id),
                    node_id=_normalize_node_id(node_id),
                    call_id=str(call_id),
                    injection_id=injection_id,
                    approved=bool(approved),
                    decision=decision,
                )

                key, meta = _approval_lookup(
                    job_id,
                    call_id,
                    node_id=node_id,
                    injection_id=injection_id,
                )

                if not meta:
                    if _is_recently_resolved_approval(job_id, call_id, node_id):
                        approval_trace(
                            "approve_command_idempotent",
                            request_id=request_id,
                            job_id=str(job_id),
                            node_id=_normalize_node_id(node_id),
                            call_id=str(call_id),
                        )
                        emit_event("exec_approval_resolved", {
                            "request_id": request_id,
                            "job_id": job_id,
                            "call_id": call_id,
                            "node_id": _normalize_node_id(node_id),
                            "injection_id": injection_id,
                            "approved": approved,
                            "decision": decision,
                            "idempotent": True,
                            "approvals_version": APPROVALS_VERSION,
                        })
                        continue

                    candidate_count = 0
                    try:
                        norm_job_id = str(job_id)
                        norm_call_id = str(call_id)
                        candidate_count = sum(
                            1
                            for key in PENDING_APPROVALS.keys()
                            if key[0] == norm_job_id and key[2] == norm_call_id
                        )
                    except Exception:
                        candidate_count = 0

                    emit_event("command_rejected", {
                        "request_id": request_id,
                        "reason": "unknown approval request",
                        "job_id": job_id,
                        "call_id": call_id,
                        "node_id": _normalize_node_id(node_id),
                        "injection_id": injection_id,
                        "pending_candidates": candidate_count,
                    })
                    approval_trace(
                        "approve_command_unknown",
                        request_id=request_id,
                        job_id=str(job_id),
                        node_id=_normalize_node_id(node_id),
                        call_id=str(call_id),
                        pending_candidates=candidate_count,
                    )
                    continue

                approval_provider = _provider_for_swarm(meta.get("swarm_id"))
                if not approval_provider:
                    emit_event("command_rejected", {
                        "request_id": request_id,
                        "reason": "provider unavailable for approval route"
                    })
                    approval_trace(
                        "approve_command_provider_missing",
                        request_id=request_id,
                        job_id=str(job_id),
                        node_id=_normalize_node_id(node_id),
                        call_id=str(call_id),
                        swarm_id=meta.get("swarm_id"),
                    )
                    continue

                try:
                    rpc_id = meta.get("rpc_id")
                    request_id_hint = meta.get("request_id_hint")
                    approval_method = meta.get("approval_method")
                    has_native_approval = bool(meta.get("has_native_approval"))
                    available_decisions = meta.get("available_decisions") or []
                    available_decisions_native = meta.get("available_decisions_native") or []
                    available_decisions_item = meta.get("available_decisions_item") or []
                    proposed_execpolicy_amendment = meta.get("proposed_execpolicy_amendment")
                    native_approval_method = approval_method in (
                        "codex/event/exec_approval_request",
                        "codex/event/apply_patch_approval_request",
                        "execCommandApproval",
                        "applyPatchApproval",
                    )
                    prefer_native_dialect = native_approval_method or has_native_approval
                    # item/* approvals are still JSON-RPC requests when they carry
                    # a top-level id. A companion native codex/event/* approval, if
                    # present, only affects decision dialect selection, not whether
                    # we owe an rpc_response.

                    route_available_decisions = (
                        available_decisions_native
                        if prefer_native_dialect
                        else available_decisions_item
                    ) or available_decisions

                    def _extract_amendment_from_decision(d):
                        if not isinstance(d, dict):
                            if isinstance(proposed_execpolicy_amendment, list):
                                return proposed_execpolicy_amendment
                            return None
                        if isinstance(d.get("approved_execpolicy_amendment"), dict):
                            inner = d["approved_execpolicy_amendment"]
                            if isinstance(inner.get("proposed_execpolicy_amendment"), list):
                                return inner["proposed_execpolicy_amendment"]
                        if isinstance(d.get("acceptWithExecpolicyAmendment"), dict):
                            inner = d["acceptWithExecpolicyAmendment"]
                            if isinstance(inner.get("execpolicy_amendment"), list):
                                return inner["execpolicy_amendment"]
                        if isinstance(proposed_execpolicy_amendment, list):
                            return proposed_execpolicy_amendment
                        return None

                    def _available_flags(available):
                        flags = {
                            "accept": False,
                            "cancel": False,
                            "approved": False,
                            "approved_for_session": False,
                            "denied": False,
                            "abort": False,
                            "accept_with_amendment": False,
                            "approved_with_amendment": False,
                        }
                        for entry in (available or []):
                            if isinstance(entry, str):
                                if entry in flags:
                                    flags[entry] = True
                            elif isinstance(entry, dict):
                                if "acceptWithExecpolicyAmendment" in entry:
                                    flags["accept_with_amendment"] = True
                                if "approved_execpolicy_amendment" in entry:
                                    flags["approved_with_amendment"] = True
                        return flags

                    def _normalize_decision_for_available(d, approved_flag, available, prefer_native_dialect=False):
                        """
                        Normalize UI decision to only use decisions explicitly present
                        in available_decisions. This prevents emitting amendment objects
                        when runtime only supports plain string decisions.
                        """
                        flags = _available_flags(available)
                        amendment = _extract_amendment_from_decision(d)

                        def _approved_plain():
                            if prefer_native_dialect:
                                if flags["approved"]:
                                    return "approved"
                                if flags["approved_for_session"]:
                                    return "approved_for_session"
                                return "approved" if approved_flag else "abort"
                            if flags["accept"]:
                                return "accept"
                            if flags["approved"]:
                                return "approved"
                            return "accept" if approved_flag else "cancel"

                        def _denied_plain():
                            if prefer_native_dialect:
                                if flags["denied"]:
                                    return "denied"
                                if flags["abort"]:
                                    return "abort"
                                return "abort" if not approved_flag else "approved"
                            if flags["cancel"]:
                                return "cancel"
                            if flags["abort"]:
                                return "abort"
                            return "cancel" if not approved_flag else "accept"

                        if approved_flag:
                            # Preserve an explicit valid string decision.
                            if isinstance(d, str):
                                if prefer_native_dialect:
                                    if d == "accept":
                                        d = "approved"
                                    elif d == "cancel":
                                        d = "abort"
                                    elif d == "acceptForSession":
                                        d = "approved_for_session"
                                if d in ("accept", "approved", "approved_for_session") and flags.get(d, False):
                                    return d
                                if d == "cancel" and flags["cancel"]:
                                    return "cancel"
                                if d == "denied" and flags["denied"]:
                                    return "denied"
                                if d == "abort" and flags["abort"]:
                                    return "abort"

                            if (
                                prefer_native_dialect
                                and isinstance(d, dict)
                                and isinstance(d.get("acceptWithExecpolicyAmendment"), dict)
                            ):
                                native_amendment = d["acceptWithExecpolicyAmendment"].get("execpolicy_amendment")
                                if isinstance(native_amendment, list):
                                    return {
                                        "approved_execpolicy_amendment": {
                                            "proposed_execpolicy_amendment": native_amendment
                                        }
                                    }

                            # Only emit amendment objects when explicitly supported.
                            if amendment:
                                if prefer_native_dialect and flags["approved_with_amendment"]:
                                    return {
                                        "approved_execpolicy_amendment": {
                                            "proposed_execpolicy_amendment": amendment
                                        }
                                    }
                                if flags["accept_with_amendment"]:
                                    return {
                                        "acceptWithExecpolicyAmendment": {
                                            "execpolicy_amendment": amendment
                                        }
                                    }
                                if flags["approved_with_amendment"]:
                                    return {
                                        "approved_execpolicy_amendment": {
                                            "proposed_execpolicy_amendment": amendment
                                        }
                                    }
                            return _approved_plain()

                        # deny path
                        if isinstance(d, str):
                            if prefer_native_dialect:
                                if d == "accept":
                                    d = "approved"
                                elif d == "cancel":
                                    d = "abort"
                                elif d == "decline":
                                    d = "denied"
                            if d in ("cancel", "abort", "denied") and flags.get(d, False):
                                return d
                            if d == "accept" and flags["accept"]:
                                return "accept"
                            if d == "approved" and flags["approved"]:
                                return "approved"
                        return _denied_plain()

                    def _coerce_to_advertised_decision(candidate, available, approved_flag, prefer_native_dialect=False):
                        """
                        Enforce protocol contract: outgoing decision must be one of
                        the options advertised in available_decisions.
                        """
                        def _decision_token(value):
                            if isinstance(value, str):
                                return ("s", value)
                            if isinstance(value, dict):
                                try:
                                    return ("j", json.dumps(value, sort_keys=True))
                                except Exception:
                                    return ("o", str(value))
                            return ("x", str(value))

                        advertised = list(available or [])
                        if not advertised:
                            return candidate

                        advertised_keys = {_decision_token(entry) for entry in advertised}
                        if _decision_token(candidate) in advertised_keys:
                            return candidate

                        if bool(approved_flag):
                            preferred = (
                                ("approved", "approved_for_session", "accept", "acceptForSession")
                                if prefer_native_dialect
                                else ("accept", "approved", "acceptForSession")
                            )
                        else:
                            preferred = (
                                ("denied", "abort", "cancel", "decline")
                                if prefer_native_dialect
                                else ("cancel", "decline", "abort")
                            )

                        for token in preferred:
                            if token in advertised:
                                return token

                        if bool(approved_flag):
                            for entry in advertised:
                                if isinstance(entry, dict):
                                    return entry

                        return advertised[0]

                    compat_rpc_payload = None
                    compat_notify_payload = None

                    # Do not silently upgrade plain approve into an amendment decision.
                    # "Approve + Remember" is an explicit user action from the UI and
                    # arrives as a dict decision payload. Keeping plain approve as a
                    # string token avoids oversized control payloads and mismatches.

                    if rpc_id is not None:
                        # JSON-RPC request-style approval (must send response with same id)
                        normalized_decision = _normalize_decision_for_available(
                            decision,
                            bool(approved),
                            route_available_decisions,
                            prefer_native_dialect,
                        )
                        normalized_decision = _coerce_to_advertised_decision(
                            normalized_decision,
                            route_available_decisions,
                            bool(approved),
                            prefer_native_dialect,
                        )

                        # Newer app-server expects an object with explicit `decision`.
                        result_payload = {
                            "decision": normalized_decision,
                        }

                        control_payload = {
                            "type": "rpc_response",
                            "rpc_id": rpc_id,
                            "result": result_payload
                        }
                        route_mode = "rpc_response"
                        # Keep notification fallback enabled; some runtimes only
                        # resume on exec/approvalResponse even when rpc_response is sent.
                        notify_decision = _normalize_decision_for_available(
                            decision,
                            bool(approved),
                            route_available_decisions,
                            prefer_native_dialect,
                        )
                        notify_decision = _coerce_to_advertised_decision(
                            notify_decision,
                            route_available_decisions,
                            bool(approved),
                            prefer_native_dialect,
                        )
                        notify_params = {
                            "call_id": call_id,
                            "callId": call_id,
                            "approved": bool(approved),
                            "decision": notify_decision,
                        }
                        if meta.get("turn_id"):
                            notify_params["id"] = meta.get("turn_id")
                            notify_params["turn_id"] = meta.get("turn_id")
                            notify_params["turnId"] = meta.get("turn_id")
                        if isinstance(notify_decision, dict):
                            notify_params.update(notify_decision)
                        if approval_method in ("applyPatchApproval", "execCommandApproval"):
                            compat_notify_payload = None
                        else:
                            compat_notify_payload = {
                                "method": "exec/approvalResponse",
                                "params": notify_params,
                            }
                        compat_rpc_payload = None
                    else:
                        # Notification-style approval
                        normalized = _normalize_decision_for_available(
                            decision,
                            bool(approved),
                            route_available_decisions,
                            prefer_native_dialect,
                        )
                        normalized = _coerce_to_advertised_decision(
                            normalized,
                            route_available_decisions,
                            bool(approved),
                            prefer_native_dialect,
                        )
                        notify_decision = normalized
                        params_payload = {
                            "call_id": call_id,
                            "callId": call_id,
                            "approved": bool(approved),
                        }
                        if meta.get("turn_id"):
                            params_payload["id"] = meta.get("turn_id")
                            params_payload["turn_id"] = meta.get("turn_id")
                            params_payload["turnId"] = meta.get("turn_id")
                        # Always include the exact normalized decision token/object
                        # chosen from advertised available_decisions.
                        if isinstance(notify_decision, dict):
                            params_payload.update(notify_decision)
                            params_payload["decision"] = notify_decision
                            params_payload["approved"] = bool(approved)
                        elif isinstance(notify_decision, str):
                            params_payload["decision"] = notify_decision
                            if notify_decision in ("accept", "acceptForSession", "approved"):
                                params_payload["approved"] = True
                            elif notify_decision in ("cancel", "decline", "abort"):
                                params_payload["approved"] = False

                        control_payload = {
                            "method": "exec/approvalResponse",
                            "params": params_payload,
                        }
                        route_mode = "exec_approval_response"

                        # Notification-style approvals are answered via
                        # exec/approvalResponse only.
                        compat_rpc_payload = None

                    is_synthetic_request = bool(
                        (meta.get("synthetic_request") is True)
                        and not bool(meta.get("has_native_approval"))
                    )

                    approval_trace(
                        "approve_command_route_send",
                        request_id=request_id,
                        job_id=str(job_id),
                        node_id=_normalize_node_id(meta.get("node_id")),
                        call_id=str(call_id),
                        route_mode=route_mode,
                        rpc_id=rpc_id,
                        synthetic=is_synthetic_request,
                    )
                    emit_event("debug", {
                        "source": "router",
                        "message": (
                            "approval route="
                            f"{route_mode} job_id={job_id} node_id={_normalize_node_id(meta.get('node_id'))} "
                            f"call_id={call_id} rpc_id={rpc_id} request_id_hint={request_id_hint} "
                            f"approval_method={approval_method} has_native={has_native_approval} "
                            f"synthetic={is_synthetic_request} "
                            f"fallback_notify={compat_notify_payload is not None}"
                        ),
                    })
                    _send_approval_decision(
                        approval_provider,
                        job_id,
                        meta["node_id"],
                        control_payload,
                        compat_rpc_payload,
                        compat_notify_payload,
                    )
                    if compat_rpc_payload is not None:
                        approval_trace(
                            "approve_command_compat_rpc_sent",
                            request_id=request_id,
                            job_id=str(job_id),
                            node_id=_normalize_node_id(meta.get("node_id")),
                            call_id=str(call_id),
                            compat_rpc_id=request_id_hint,
                        )
                    if compat_notify_payload is not None:
                        approval_trace(
                            "approve_command_compat_notify_sent",
                            request_id=request_id,
                            job_id=str(job_id),
                            node_id=_normalize_node_id(meta.get("node_id")),
                            call_id=str(call_id),
                            method="exec/approvalResponse",
                        )

                    persisted_decision = normalized_decision if rpc_id is not None else normalized
                    PENDING_APPROVAL_DECISIONS[key] = {
                        "request_id": request_id,
                        "approved": bool(approved),
                        "decision": persisted_decision,
                        "job_id": str(job_id),
                        "node_id": meta.get("node_id"),
                        "control_payload": control_payload,
                        "compat_rpc_payload": compat_rpc_payload,
                        "compat_notify_payload": compat_notify_payload,
                        "timestamp": time.time(),
                        "last_sent_ts": time.time(),
                        "send_attempts": 1,
                    }
                except Exception as e:
                    approval_trace(
                        "approve_command_route_error",
                        request_id=request_id,
                        job_id=str(job_id),
                        node_id=_normalize_node_id(meta.get("node_id")) if meta else _normalize_node_id(node_id),
                        call_id=str(call_id),
                        error=str(e),
                    )
                    emit_event("command_rejected", {
                        "request_id": request_id,
                        "reason": str(e)
                    })
                    continue

                approval_id = meta.get("approval_id")
                PENDING_APPROVALS[key]["approval_status"] = (
                    "approved_pending_ack" if bool(approved) else "denied_pending_ack"
                )
                PENDING_APPROVALS[key]["updated_at_ts"] = time.time()
                approvals_version = _bump_approvals_version()
                emit_event("exec_approval_submitted", {
                    "request_id": request_id,
                    "approval_id": approval_id,
                    "job_id": job_id,
                    "call_id": call_id,
                    "node_id": meta.get("node_id"),
                    "injection_id": meta.get("injection_id"),
                    "approved": approved,
                    "decision": decision,
                    "approvals_version": approvals_version,
                })
                approval_trace(
                    "approve_command_submitted_pending_ack",
                    request_id=request_id,
                    approval_id=approval_id,
                    approvals_version=approvals_version,
                    job_id=str(job_id),
                    node_id=_normalize_node_id(meta.get("node_id")),
                    call_id=str(call_id),
                    approved=bool(approved),
                    synthetic=is_synthetic_request,
                )

            elif command == "swarm_terminate":
                swarm_id = payload.get("swarm_id")
                terminate_params = payload.get("terminate_params")
                if not isinstance(terminate_params, dict):
                    terminate_params = None
                swarm = SWARMS.get(swarm_id)
                if not swarm:
                    emit_event("command_rejected", {
                        "request_id": request_id,
                        "reason": "unknown swarm_id"
                    })
                    continue

                with SCHEDULER_LOCK:
                    if str(swarm_id) in TERMINATION_IN_PROGRESS:
                        # Repeated terminate requests escalate to forced termination.
                        FORCE_TERMINATION_REQUESTED.add(str(swarm_id))
                        # Treat repeated terminate requests as idempotent while
                        # termination is already underway.
                        emit_event("swarm_status", {
                            "request_id": request_id,
                            "swarm_id": swarm_id,
                            "job_id": swarm.get("job_id"),
                            "node_count": swarm.get("node_count"),
                            "status": "terminating"
                        })
                        continue
                    TERMINATION_IN_PROGRESS.add(str(swarm_id))

                if swarm.get("status") != "terminated":
                    swarm["status"] = "terminating"
                    swarm["terminating_since"] = time.time()
                    save_state()

                emit_event("swarm_status", {
                    "request_id": request_id,
                    "swarm_id": swarm_id,
                    "job_id": swarm.get("job_id"),
                    "node_count": swarm.get("node_count"),
                    "status": "terminating"
                })

                terminate_provider = _provider_for_swarm(swarm_id)
                if not terminate_provider:
                    with SCHEDULER_LOCK:
                        TERMINATION_IN_PROGRESS.discard(str(swarm_id))
                    emit_event("command_rejected", {
                        "request_id": request_id,
                        "reason": "provider unavailable for termination"
                    })
                    continue

                threading.Thread(
                    target=_graceful_terminate_swarm,
                    args=(config, terminate_provider, request_id, str(swarm_id), terminate_params),
                    daemon=True
                ).start()
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

    if args.daemon:
        write_pid_file()
        atexit.register(remove_pid_file)

    config = load_config(args.config)

    # Store absolute config path so swarm_launch uses the same config
    config["_config_path"] = str(Path(args.config).resolve())

    # Build provider catalog/instances
    global PROVIDER_SPECS, PROVIDERS
    PROVIDER_SPECS = get_provider_specs(config)
    PROVIDERS = build_providers(config, PROVIDER_SPECS)

    # Load persisted state and reconcile with cluster backends
    load_state()

    for provider in PROVIDERS.values():
        try:
            recovered = provider.recover_swarms()
        except Exception:
            recovered = {}
        if not isinstance(recovered, dict):
            continue
        for swarm_id, swarm in recovered.items():
            if not isinstance(swarm_id, str) or not swarm_id:
                continue
            if not isinstance(swarm, dict):
                continue
            existing = SWARMS.get(swarm_id)
            if existing:
                merged = dict(swarm)
                merged.update(existing)
                SWARMS[swarm_id] = merged
            else:
                SWARMS[swarm_id] = dict(swarm)

    # Migration: ensure terminated swarms have terminated_at and provider binding.
    default_provider_ref = PROVIDER_SPECS[0].get("provider_ref") if PROVIDER_SPECS else None
    default_provider_backend = PROVIDER_SPECS[0].get("backend") if PROVIDER_SPECS else None
    for swarm in SWARMS.values():
        if swarm.get("status") == "terminated" and "terminated_at" not in swarm:
            swarm["terminated_at"] = time.time()
        if "provider" not in swarm:
            if swarm.get("backend"):
                swarm["provider"] = swarm.get("backend")
            elif default_provider_ref:
                swarm["provider"] = default_provider_ref
        if "provider_backend" not in swarm:
            if swarm.get("backend"):
                swarm["provider_backend"] = swarm.get("backend")
            elif default_provider_backend:
                swarm["provider_backend"] = default_provider_backend
    save_state()

    reconcile(PROVIDERS)

    # Ensure state is flushed on shutdown
    import signal

    def graceful_shutdown(signum, frame):
        save_state()
        remove_pid_file()
        sys.exit(0)

    signal.signal(signal.SIGTERM, graceful_shutdown)
    signal.signal(signal.SIGINT, graceful_shutdown)

    if args.daemon:
        run_daemon(config, PROVIDERS)


if __name__ == "__main__":
    main()
