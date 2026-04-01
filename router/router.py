import argparse
import subprocess
import json
import sys
import uuid
import shlex
import os
import select
import copy
import tempfile
import hashlib
from pathlib import Path
from datetime import datetime, timezone
import threading
import re
import time
import atexit
import shutil
from collections import defaultdict, deque
from decimal import Decimal, InvalidOperation

sys.path.append(str(Path(__file__).resolve().parents[1]))
from common.config import load_config
from .providers.factory import build_providers, get_provider_specs
from .providers.claude_env import resolve_claude_profile_model as _resolve_provider_claude_profile_model


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
MODEL_PRICING = {}
NODE_OUTSTANDING = defaultdict(int)
NODE_THREAD_ACTIVE = defaultdict(bool)
FINAL_ANSWER_SEEN = set()
INTER_SWARM_QUEUE = defaultdict(deque)
PROJECTS = {}
PENDING_PROJECT_PLANS = {}
SCHEDULER_LOCK = threading.Lock()
STATE_SAVE_LOCK = threading.Lock()
TERMINATION_IN_PROGRESS = set()
FORCE_TERMINATION_REQUESTED = set()
PROJECT_BEADS_PERSIST_LOCKS = defaultdict(threading.Lock)

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
PROJECT_REPO_CACHE_ROOT = Path(__file__).resolve().parents[1] / ".tmp" / "project_repos"
PROJECT_REPO_CACHE_ROOT = Path(__file__).resolve().parents[1] / ".tmp" / "project_repos"
PROJECT_CONTROL_BRANCH_PREFIX = "codeswarm/project-control"
STARTUP_RECONCILE_TIMEOUT_SECONDS = 20.0
LOCAL_STARTUP_RECONCILE_TIMEOUT_SECONDS = 5.0
SLURM_STARTUP_RECONCILE_TIMEOUT_SECONDS = 60.0
AWS_STARTUP_RECONCILE_TIMEOUT_SECONDS = 60.0

DEFAULT_MODEL_PRICING = {
    "gpt-5.4": {
        "input_tokens_usd_per_m": 2.5,
        "cached_input_tokens_usd_per_m": 0.25,
        "output_tokens_usd_per_m": 15.0,
        "reasoning_output_tokens_usd_per_m": 0.0,
    }
}


def _pid_file_path():
    raw = str(os.environ.get("CODESWARM_ROUTER_PID_FILE") or "").strip()
    return Path(raw).expanduser() if raw else PID_FILE


def _state_file_path():
    raw = str(os.environ.get("CODESWARM_ROUTER_STATE_FILE") or "").strip()
    return Path(raw).expanduser() if raw else STATE_FILE


def _project_repo_cache_root():
    raw = str(os.environ.get("CODESWARM_PROJECT_REPO_CACHE_ROOT") or "").strip()
    return Path(raw).expanduser() if raw else PROJECT_REPO_CACHE_ROOT


def _project_repo_cache_root():
    raw = str(os.environ.get("CODESWARM_PROJECT_REPO_CACHE_ROOT") or "").strip()
    return Path(raw).expanduser() if raw else PROJECT_REPO_CACHE_ROOT


def _project_control_cache_root():
    return _project_repo_cache_root() / "_control"


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


def _approval_prefers_native_dialect(approval_method, has_native_approval=False):
    method = str(approval_method or "")
    if method in (
        "codex/event/exec_approval_request",
        "codex/event/apply_patch_approval_request",
        "execCommandApproval",
        "applyPatchApproval",
    ):
        return True
    if method in (
        "item/commandExecution/requestApproval",
        "item/fileChange/requestApproval",
    ):
        return False
    return bool(has_native_approval)


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
    available_decisions=None,
    turn_id=None,
):
    # Replay resolved approvals using the exact stored decision token/object.
    try:
        replay_decision = decision
        if replay_decision is None:
            replay_decision = "approved" if bool(approved) else "abort"

        prefer_native_dialect = _approval_prefers_native_dialect(
            approval_method,
            has_native_approval,
        )
        route_available_decisions = list(available_decisions or [])
        if route_available_decisions:
            replay_decision = _normalize_decision_for_available(
                replay_decision,
                bool(approved),
                route_available_decisions,
                prefer_native_dialect,
            )
            replay_decision = _coerce_to_advertised_decision(
                replay_decision,
                route_available_decisions,
                bool(approved),
                prefer_native_dialect,
            )

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


def _register_canonical_exec_approval(data):
    if not isinstance(data, dict):
        return None
    job_id = str(data.get("job_id") or "")
    swarm_id = str(data.get("swarm_id") or "")
    node_id = data.get("node_id")
    call_id = str(data.get("call_id") or "").strip()
    if not job_id or not swarm_id or not isinstance(node_id, int) or node_id < 0 or not call_id:
        return None

    key = _approval_key(job_id, call_id, node_id)
    existing = PENDING_APPROVALS.get(key)
    if existing:
        existing["updated_at_ts"] = time.time()
        if isinstance(data.get("command"), (str, list, dict)):
            existing["command"] = data.get("command")
        if isinstance(data.get("reason"), str):
            existing["reason"] = data.get("reason")
        if isinstance(data.get("cwd"), str):
            existing["cwd"] = data.get("cwd")
        if isinstance(data.get("available_decisions"), list):
            existing["available_decisions"] = list(data.get("available_decisions") or [])
            existing["available_decisions_item"] = list(data.get("available_decisions") or [])
        return existing.get("approval_id")

    if _is_recently_resolved_approval(job_id, call_id, node_id):
        return None

    approval_id = str(uuid.uuid4())
    available_decisions = (
        list(data.get("available_decisions") or [])
        if isinstance(data.get("available_decisions"), list)
        else ["accept", "cancel"]
    )
    PENDING_APPROVALS[key] = {
        "approval_id": approval_id,
        "approval_status": "pending",
        "swarm_id": swarm_id,
        "node_id": node_id,
        "injection_id": data.get("injection_id"),
        "turn_id": data.get("turn_id"),
        "rpc_id": None,
        "request_id_hint": None,
        "approval_method": str(data.get("approval_method") or "worker/exec_approval_required"),
        "command": data.get("command"),
        "reason": data.get("reason"),
        "cwd": data.get("cwd"),
        "proposed_execpolicy_amendment": data.get("proposed_execpolicy_amendment"),
        "available_decisions": available_decisions,
        "available_decisions_native": [],
        "available_decisions_item": available_decisions,
        "synthetic_request": False,
        "has_native_approval": False,
        "created_at_ts": time.time(),
        "updated_at_ts": time.time(),
    }
    _bump_approvals_version()
    return approval_id


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
        state_file = _state_file_path()
        # Serialize save operations so concurrent callers cannot race on the
        # temporary file/rename path.
        with STATE_SAVE_LOCK:
            # Snapshot mutable in-memory structures while holding the scheduler lock
            # so serialization cannot race with concurrent updates.
            with SCHEDULER_LOCK:
                swarms_snapshot = copy.deepcopy(SWARMS)
                projects_snapshot = copy.deepcopy(PROJECTS)
                plans_snapshot = copy.deepcopy(PENDING_PROJECT_PLANS)
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
                "projects": projects_snapshot,
                "pending_project_plans": plans_snapshot,
                "inter_swarm_queue": queue_snapshot,
            }

            state_file.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                prefix=f"{state_file.name}.",
                suffix=".tmp",
                dir=str(state_file.parent),
            )
            tmp_path = Path(tmp_name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, state_file)
            finally:
                try:
                    if tmp_path.exists():
                        tmp_path.unlink()
                except Exception:
                    pass
    except Exception as e:
        print(f"[router ERROR] failed to save state to {_state_file_path()}: {e}", file=sys.stderr, flush=True)


def load_state():
    global SWARMS, INTER_SWARM_QUEUE, PROJECTS, PENDING_PROJECT_PLANS
    try:
        state_file = _state_file_path()
        if state_file.exists():
            with open(state_file) as f:
                data = json.load(f)
                SWARMS = data.get("swarms", {})
                PROJECTS = data.get("projects", {}) if isinstance(data.get("projects"), dict) else {}
                PENDING_PROJECT_PLANS = (
                    data.get("pending_project_plans", {})
                    if isinstance(data.get("pending_project_plans"), dict)
                    else {}
                )
                for project in PROJECTS.values():
                    if not isinstance(project, dict):
                        continue
                    tasks = project.get("tasks")
                    if not isinstance(tasks, dict):
                        continue
                    for task in tasks.values():
                        if not isinstance(task, dict):
                            continue
                        if task.get("status") == "assigned":
                            task["status"] = "pending"
                            task["assigned_swarm_id"] = None
                            task["assigned_node_id"] = None
                            task["assignment_injection_id"] = None
                            task["updated_at"] = time.time()
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
        PROJECTS = {}
        PENDING_PROJECT_PLANS = {}
        INTER_SWARM_QUEUE = defaultdict(deque)


def write_pid_file():
    try:
        _pid_file_path().write_text(f"{os.getpid()}\n", encoding="utf-8")
    except Exception:
        pass


def remove_pid_file():
    try:
        path = _pid_file_path()
        if path.exists():
            raw = path.read_text(encoding="utf-8").strip()
            if raw == str(os.getpid()):
                path.unlink()
    except Exception:
        pass


def _startup_reconcile_timeout_seconds(config, provider_ref):
    router_cfg = config.get("router") if isinstance(config, dict) else {}
    router_cfg = router_cfg if isinstance(router_cfg, dict) else {}
    backend = str(provider_ref or "").split(":", 1)[0].strip().lower()

    backend_key_map = {
        "local": "local_startup_reconcile_timeout_seconds",
        "slurm": "slurm_startup_reconcile_timeout_seconds",
        "aws": "aws_startup_reconcile_timeout_seconds",
    }
    raw_value = router_cfg.get(backend_key_map.get(backend, ""))
    if raw_value is None:
        raw_value = router_cfg.get("startup_reconcile_timeout_seconds")

    try:
        resolved = float(raw_value)
    except Exception:
        resolved = None

    if resolved is not None and resolved > 0:
        return resolved
    if backend == "local":
        return LOCAL_STARTUP_RECONCILE_TIMEOUT_SECONDS
    if backend == "slurm":
        return SLURM_STARTUP_RECONCILE_TIMEOUT_SECONDS
    if backend == "aws":
        return AWS_STARTUP_RECONCILE_TIMEOUT_SECONDS
    return STARTUP_RECONCILE_TIMEOUT_SECONDS


def _set_provider_spec_disabled_state(provider_ref, disabled, reason=None):
    normalized_ref = str(provider_ref or "").strip().lower()
    if not normalized_ref:
        return
    resolved_reason = str(reason).strip() if reason is not None and str(reason).strip() else None
    for spec in PROVIDER_SPECS:
        spec_ref = str(spec.get("provider_ref") or "").strip().lower()
        if spec_ref != normalized_ref:
            continue
        spec["disabled"] = bool(disabled)
        spec["disabled_reason"] = resolved_reason if disabled else None


def reconcile(providers, config=None):
    global JOB_TO_SWARM
    running_jobs_by_provider = {}
    provider_threads = {}
    provider_results = {}
    provider_errors = {}

    def _list_provider_jobs(provider_ref, provider):
        try:
            provider_results[provider_ref] = provider.list_active_jobs()
        except Exception as e:
            provider_errors[provider_ref] = str(e)

    for provider_ref, provider in providers.items():
        timeout_s = _startup_reconcile_timeout_seconds(config, provider_ref)
        startup_log(f"reconciling provider {provider_ref} (timeout {int(timeout_s)}s)")
        thread = threading.Thread(
            target=_list_provider_jobs,
            args=(provider_ref, provider),
            daemon=True,
        )
        provider_threads[provider_ref] = (thread, timeout_s)
        thread.start()

    for provider_ref, (thread, timeout_s) in provider_threads.items():
        thread.join(timeout_s)
        if thread.is_alive():
            reason = f"Provider reconcile timed out after {int(timeout_s)}s during router startup"
            _set_provider_spec_disabled_state(provider_ref, True, reason)
            startup_log(
                f"provider {provider_ref} did not finish reconcile within {int(timeout_s)}s; continuing with empty active-job set"
            )
            running_jobs_by_provider[provider_ref] = {}
            continue
        error = provider_errors.get(provider_ref)
        if error:
            _set_provider_spec_disabled_state(provider_ref, True, f"Provider reconcile failed during router startup: {error}")
            startup_log(f"provider {provider_ref} reconcile failed: {error}")
            running_jobs_by_provider[provider_ref] = {}
            continue
        jobs = provider_results.get(provider_ref)
        _set_provider_spec_disabled_state(provider_ref, False, None)
        running_jobs_by_provider[provider_ref] = jobs if isinstance(jobs, dict) else {}
        startup_log(
            f"provider {provider_ref} reconcile complete ({len(running_jobs_by_provider[provider_ref])} active job(s))"
        )

    JOB_TO_SWARM.clear()

    stale_swarms = []

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
            stale_swarms.append((swarm_id, job_id))

    approvals_changed = False
    for swarm_id, job_id in stale_swarms:
        swarm = SWARMS.pop(str(swarm_id), None)
        if job_id:
            JOB_TO_SWARM.pop(str(job_id), None)

        with SCHEDULER_LOCK:
            node_count = int((swarm or {}).get("node_count") or 0)
            for node_id in range(node_count):
                key = _node_key(swarm_id, node_id)
                NODE_THREAD_ACTIVE.pop(key, None)
                NODE_OUTSTANDING.pop(key, None)

            stale_final_keys = [
                key for key in FINAL_ANSWER_SEEN
                if isinstance(key, tuple) and len(key) == 3 and str(key[0]) == str(swarm_id)
            ]
            for key in stale_final_keys:
                FINAL_ANSWER_SEEN.discard(key)

            dropped = list(INTER_SWARM_QUEUE.pop(str(swarm_id), []))

        for item in dropped:
            emit_event("inter_swarm_dropped", {
                "queue_id": item.get("queue_id"),
                "request_id": item.get("request_id"),
                "source_swarm_id": item.get("source_swarm_id"),
                "target_swarm_id": str(swarm_id),
                "reason": "target swarm unavailable during reconcile",
            })

        if job_id:
            stale_approvals = [
                key for key in list(PENDING_APPROVALS.keys())
                if isinstance(key, tuple) and len(key) == 3 and str(key[0]) == str(job_id)
            ]
            for key in stale_approvals:
                PENDING_APPROVALS.pop(key, None)
                PENDING_APPROVAL_DECISIONS.pop(key, None)
                RESOLVED_APPROVALS.pop(key, None)
                RECENT_APPROVAL_RESULTS.pop(key, None)
                approvals_changed = True

        emit_event("swarm_removed", {
            "swarm_id": str(swarm_id),
            "reason": "provider reported no active job during reconcile",
        })

    if approvals_changed:
        _bump_approvals_version()

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


def _to_decimal(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float, str)):
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None
    return None


def _round_cost_decimal(value):
    number = _to_decimal(value)
    if number is None:
        return None
    return float(number.quantize(Decimal("0.000000000001")))


def _normalize_model_key(value):
    text = str(value or "").strip()
    if not text:
        return ""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _normalize_model_pricing(raw_catalog):
    catalog = {}
    if not isinstance(raw_catalog, dict):
        return catalog
    for raw_name, raw_entry in raw_catalog.items():
        model_name = str(raw_name or "").strip()
        if not model_name or not isinstance(raw_entry, dict):
            continue
        entry = {}
        for source_key, dest_key in (
            ("input_tokens_usd_per_m", "input_tokens_usd_per_m"),
            ("cached_input_tokens_usd_per_m", "cached_input_tokens_usd_per_m"),
            ("output_tokens_usd_per_m", "output_tokens_usd_per_m"),
            ("reasoning_output_tokens_usd_per_m", "reasoning_output_tokens_usd_per_m"),
        ):
            value = _to_decimal(raw_entry.get(source_key))
            if value is None:
                continue
            entry[dest_key] = float(value)
        if not entry:
            continue
        catalog[_normalize_model_key(model_name)] = {
            "model_name": model_name,
            **entry,
        }
    return catalog


def _load_model_pricing_catalog(config):
    catalog = _normalize_model_pricing(DEFAULT_MODEL_PRICING)
    configured = _normalize_model_pricing((config or {}).get("model_pricing"))
    catalog.update(configured)
    return catalog


def _pricing_entry_for_model(model_name):
    key = _normalize_model_key(model_name)
    if not key:
        return None
    return MODEL_PRICING.get(key)


def _resolve_claude_profile_model(config, profile_name, provider_backend=None):
    text = str(profile_name or "").strip()
    if not text:
        return None
    cluster_cfg = ((config or {}).get("cluster") or {})
    if not isinstance(cluster_cfg, dict):
        return None
    candidates: list[dict] = []
    backend_text = str(provider_backend or "").strip().lower()
    if backend_text:
        backend_cfg = cluster_cfg.get(backend_text) or {}
        if isinstance(backend_cfg, dict):
            candidates.append(backend_cfg)
        elif str(cluster_cfg.get("backend") or "").strip().lower() == backend_text:
            candidates.append(cluster_cfg)
    else:
        active_backend = str(cluster_cfg.get("backend") or "").strip().lower()
        if active_backend:
            active_cfg = cluster_cfg.get(active_backend) or {}
            if isinstance(active_cfg, dict):
                candidates.append(active_cfg)
            else:
                candidates.append(cluster_cfg)
        for backend_key in ("local", "aws"):
            backend_cfg = cluster_cfg.get(backend_key) or {}
            if isinstance(backend_cfg, dict):
                candidates.append(backend_cfg)
    for candidate in candidates:
        resolved = _resolve_provider_claude_profile_model(candidate, text)
        if resolved:
            return resolved
    return None


def _resolve_swarm_agent_model(config, agent_runtime, launch_params, provider_backend=None):
    params = launch_params if isinstance(launch_params, dict) else {}
    runtime = str(agent_runtime or params.get("agent_runtime") or params.get("worker_mode") or "").strip().lower()
    for key in ("agent_model", "model"):
        value = str(params.get(key) or "").strip()
        if value:
            return value
    if runtime == "claude":
        explicit = str(params.get("claude_model") or "").strip()
        if explicit:
            return explicit
        from_profile = _resolve_claude_profile_model(config, params.get("claude_env_profile"), provider_backend=provider_backend)
        if from_profile:
            return from_profile
    return None


def _resolve_swarm_pricing_model(config, agent_runtime, launch_params, agent_model=None, provider_backend=None):
    params = launch_params if isinstance(launch_params, dict) else {}
    explicit = str(params.get("pricing_model") or "").strip()
    if explicit:
        return explicit
    if isinstance(agent_model, str) and agent_model.strip():
        return agent_model.strip()
    runtime = str(agent_runtime or params.get("agent_runtime") or params.get("worker_mode") or "").strip().lower()
    if runtime == "codex":
        return "gpt-5.4"
    if runtime == "claude":
        from_profile = _resolve_claude_profile_model(config, params.get("claude_env_profile"), provider_backend=provider_backend)
        if from_profile:
            return from_profile
    return None


def _estimate_usage_cost_usd(token_usage, pricing_entry):
    if not isinstance(token_usage, dict) or not isinstance(pricing_entry, dict):
        return None
    million = Decimal("1000000")
    total = Decimal("0")
    input_tokens = Decimal(max(0, _to_int(token_usage.get("input_tokens")) or 0))
    cached_input_tokens = Decimal(max(0, _to_int(token_usage.get("cached_input_tokens")) or 0))
    non_cached_input = max(Decimal("0"), input_tokens - cached_input_tokens)
    output_tokens = Decimal(max(0, _to_int(token_usage.get("output_tokens")) or 0))
    reasoning_output_tokens = Decimal(max(0, _to_int(token_usage.get("reasoning_output_tokens")) or 0))

    components = (
        (non_cached_input, pricing_entry.get("input_tokens_usd_per_m")),
        (cached_input_tokens, pricing_entry.get("cached_input_tokens_usd_per_m")),
        (output_tokens, pricing_entry.get("output_tokens_usd_per_m")),
        (reasoning_output_tokens, pricing_entry.get("reasoning_output_tokens_usd_per_m")),
    )
    for tokens, raw_rate in components:
        rate = _to_decimal(raw_rate)
        if rate is None or tokens <= 0:
            continue
        total += (tokens / million) * rate
    return _round_cost_decimal(total)


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

    swarm = SWARMS.get(str((base or {}).get("swarm_id") or ""))
    model_name = str(((swarm or {}).get("agent_model")) or "").strip() or None
    pricing_model = str(((swarm or {}).get("pricing_model")) or "").strip() or model_name or None
    pricing_entry = _pricing_entry_for_model(pricing_model or model_name)
    total_estimated_cost_usd = _estimate_usage_cost_usd(
        {
            "input_tokens": input_tokens,
            "cached_input_tokens": cached_input_tokens,
            "output_tokens": output_tokens,
            "reasoning_output_tokens": reasoning_output_tokens,
        },
        pricing_entry,
    )
    last_estimated_cost_usd = _estimate_usage_cost_usd(
        {
            "input_tokens": last_input_tokens,
            "cached_input_tokens": last_cached_input_tokens,
            "output_tokens": last_output_tokens,
            "reasoning_output_tokens": last_reasoning_output_tokens,
        },
        pricing_entry,
    )

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
        "model_name": model_name,
        "pricing_model": pricing_model,
        "estimated_cost_usd": total_estimated_cost_usd,
        "last_estimated_cost_usd": last_estimated_cost_usd,
    }


USAGE_COUNTER_FIELDS = (
    "total_tokens",
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)

USAGE_LAST_FIELD_MAP = {
    "total_tokens": "last_total_tokens",
    "input_tokens": "last_input_tokens",
    "cached_input_tokens": "last_cached_input_tokens",
    "output_tokens": "last_output_tokens",
    "reasoning_output_tokens": "last_reasoning_output_tokens",
}


def _empty_usage_totals():
    return {field: 0 for field in USAGE_COUNTER_FIELDS}


def _normalize_usage_snapshot(payload):
    if not isinstance(payload, dict):
        return None
    total_tokens = _to_int(payload.get("total_tokens"))
    if total_tokens is None:
        return None
    snapshot = _empty_usage_totals()
    snapshot["total_tokens"] = total_tokens
    for field in USAGE_COUNTER_FIELDS:
        if field == "total_tokens":
            continue
        value = _to_int(payload.get(field))
        snapshot[field] = value if value is not None else 0
    model_context_window = _to_int(payload.get("model_context_window"))
    if model_context_window is not None:
        snapshot["model_context_window"] = model_context_window
    usage_source = str(payload.get("usage_source") or "").strip()
    if usage_source:
        snapshot["usage_source"] = usage_source
    model_name = str(payload.get("model_name") or "").strip()
    if model_name:
        snapshot["model_name"] = model_name
    pricing_model = str(payload.get("pricing_model") or "").strip()
    if pricing_model:
        snapshot["pricing_model"] = pricing_model
    estimated_cost_usd = _to_decimal(payload.get("estimated_cost_usd"))
    if estimated_cost_usd is not None:
        snapshot["estimated_cost_usd"] = _round_cost_decimal(estimated_cost_usd)
    return snapshot


def _usage_delta(current_snapshot, previous_snapshot):
    if not isinstance(current_snapshot, dict):
        return None
    previous = previous_snapshot if isinstance(previous_snapshot, dict) else {}
    delta = _empty_usage_totals()
    changed = False
    for field in USAGE_COUNTER_FIELDS:
        current_value = _to_int(current_snapshot.get(field)) or 0
        previous_value = _to_int(previous.get(field)) or 0
        field_delta = current_value - previous_value
        if field_delta < 0:
            field_delta = current_value
        delta[field] = field_delta
        if field_delta:
            changed = True
    if not changed:
        cost_current = _to_decimal(current_snapshot.get("estimated_cost_usd"))
        cost_previous = _to_decimal(previous.get("estimated_cost_usd"))
        if cost_current is None:
            return None
        cost_delta = cost_current - (cost_previous or Decimal("0"))
        if cost_delta < 0:
            cost_delta = cost_current
        if cost_delta <= 0:
            return None
        delta["estimated_cost_usd"] = _round_cost_decimal(cost_delta)
    else:
        cost_current = _to_decimal(current_snapshot.get("estimated_cost_usd"))
        cost_previous = _to_decimal(previous.get("estimated_cost_usd"))
        if cost_current is not None:
            cost_delta = cost_current - (cost_previous or Decimal("0"))
            if cost_delta < 0:
                cost_delta = cost_current
            delta["estimated_cost_usd"] = _round_cost_decimal(cost_delta)
    if "model_context_window" in current_snapshot:
        delta["model_context_window"] = current_snapshot.get("model_context_window")
    if "usage_source" in current_snapshot:
        delta["usage_source"] = current_snapshot.get("usage_source")
    if "model_name" in current_snapshot:
        delta["model_name"] = current_snapshot.get("model_name")
    if "pricing_model" in current_snapshot:
        delta["pricing_model"] = current_snapshot.get("pricing_model")
    return delta


def _usage_delta_for_project_accounting(current_snapshot, previous_snapshot, payload):
    if not isinstance(current_snapshot, dict):
        return None
    payload_dict = payload if isinstance(payload, dict) else {}
    delta = _empty_usage_totals()
    saw_last_value = False
    changed = False
    for field, last_field in USAGE_LAST_FIELD_MAP.items():
        last_value = _to_int(payload_dict.get(last_field))
        if last_value is None:
            continue
        saw_last_value = True
        field_delta = max(0, last_value)
        delta[field] = field_delta
        if field_delta:
            changed = True
    if saw_last_value:
        if not changed:
            last_cost = _to_decimal(payload_dict.get("last_estimated_cost_usd"))
            if last_cost is None or last_cost <= 0:
                return None
            delta["estimated_cost_usd"] = _round_cost_decimal(last_cost)
        else:
            last_cost = _to_decimal(payload_dict.get("last_estimated_cost_usd"))
            if last_cost is not None:
                delta["estimated_cost_usd"] = _round_cost_decimal(max(Decimal("0"), last_cost))
        if "model_context_window" in current_snapshot:
            delta["model_context_window"] = current_snapshot.get("model_context_window")
        if "usage_source" in current_snapshot:
            delta["usage_source"] = current_snapshot.get("usage_source")
        if "model_name" in current_snapshot:
            delta["model_name"] = current_snapshot.get("model_name")
        if "pricing_model" in current_snapshot:
            delta["pricing_model"] = current_snapshot.get("pricing_model")
        return delta
    return _usage_delta(current_snapshot, previous_snapshot)


def _apply_usage_delta(record, delta):
    if not isinstance(record, dict) or not isinstance(delta, dict):
        return False
    usage = record.get("usage")
    if not isinstance(usage, dict):
        usage = _empty_usage_totals()
    changed = False
    for field in USAGE_COUNTER_FIELDS:
        current_value = _to_int(usage.get(field)) or 0
        next_value = current_value + (_to_int(delta.get(field)) or 0)
        if next_value != current_value:
            changed = True
        usage[field] = next_value
    current_cost = _to_decimal(usage.get("estimated_cost_usd"))
    delta_cost = _to_decimal(delta.get("estimated_cost_usd"))
    if current_cost is not None or delta_cost is not None:
        current_cost_value = current_cost or Decimal("0")
        delta_cost_value = delta_cost or Decimal("0")
        next_cost = current_cost_value + delta_cost_value
        if next_cost != current_cost_value:
            changed = True
        usage["estimated_cost_usd"] = _round_cost_decimal(next_cost)
    model_context_window = _to_int(delta.get("model_context_window"))
    if model_context_window is not None:
        usage["model_context_window"] = model_context_window
    usage_source = str(delta.get("usage_source") or "").strip()
    if usage_source:
        usage["usage_source"] = usage_source
    delta_model_name = str(delta.get("model_name") or "").strip()
    existing_model_name = str(usage.get("model_name") or "").strip()
    if delta_model_name:
        if not existing_model_name:
            usage["model_name"] = delta_model_name
        elif existing_model_name != delta_model_name and existing_model_name != "mixed":
            usage["model_name"] = "mixed"
            changed = True
    delta_pricing_model = str(delta.get("pricing_model") or "").strip()
    existing_pricing_model = str(usage.get("pricing_model") or "").strip()
    if delta_pricing_model:
        if not existing_pricing_model:
            usage["pricing_model"] = delta_pricing_model
        elif existing_pricing_model != delta_pricing_model and existing_pricing_model != "mixed":
            usage["pricing_model"] = "mixed"
            changed = True
    record["usage"] = usage
    return changed


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
    with TCP_CLIENTS_LOCK:
        clients = list(TCP_CLIENTS)

    for conn in clients:
        try:
            conn.sendall(line.encode())
        except:
            dead.append(conn)

    if dead:
        with TCP_CLIENTS_LOCK:
            for conn in dead:
                if conn in TCP_CLIENTS:
                    TCP_CLIENTS.remove(conn)

    if DEBUG:
        print(line, end="", flush=True)


def debug_event(message):
    if DEBUG:
        emit_event("debug", {"source": "router", "message": message})


def startup_log(message):
    print(f"[router startup] {message}", file=sys.stderr, flush=True)


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


def _project_task_paths(task):
    for key in ("owned_paths", "expected_touch_paths"):
        value = task.get(key)
        if isinstance(value, list):
            return [str(item).strip("/") for item in value if isinstance(item, str) and item.strip("/")]
    return []


def _paths_overlap(left_paths, right_paths):
    for left in left_paths:
        for right in right_paths:
            if left == right:
                return True
            if left.startswith(f"{right}/") or right.startswith(f"{left}/"):
                return True
    return False


def _sanitize_branch_token(value):
    text = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(value or "").strip())
    text = text.strip("-._")
    return text or "task"


def _parse_github_repo_ref(value):
    text = str(value or "").strip()
    if not text:
        return None
    shorthand = re.fullmatch(r"([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)", text)
    if shorthand:
        return f"{shorthand.group(1)}/{shorthand.group(2)}"
    https_match = re.fullmatch(
        r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?",
        text,
    )
    if https_match:
        return f"{https_match.group(1)}/{https_match.group(2)}"
    ssh_match = re.fullmatch(
        r"git@github\.com:([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?",
        text,
    )
    if ssh_match:
        return f"{ssh_match.group(1)}/{ssh_match.group(2)}"
    return None


def _git_origin_remote_url(repo_path: Path):
    if not repo_path.exists() or not (repo_path / ".git").exists():
        return None
    result = subprocess.run(
        ["git", "-C", str(repo_path), "config", "--get", "remote.origin.url"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    value = str(result.stdout or "").strip()
    return value or None


def _remove_path(path: Path):
    if not path.exists():
        return
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def _run_gh(args):
    if not shutil.which("gh"):
        return None, "gh CLI is not installed"
    try:
        completed = subprocess.run(
            ["gh", *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except Exception as e:
        return None, str(e)
    if completed.returncode != 0:
        return None, (completed.stderr or completed.stdout or f"exit {completed.returncode}").strip()
    return completed, None


def _github_repo_metadata(name_with_owner):
    completed, error = _run_gh([
        "repo",
        "view",
        name_with_owner,
        "--json",
        "nameWithOwner,sshUrl,url,defaultBranchRef,visibility",
    ])
    if not completed:
        return None, error
    try:
        parsed = json.loads(completed.stdout or "{}")
    except Exception as e:
        return None, f"Failed to parse gh repo view output: {e}"
    if not isinstance(parsed, dict):
        return None, "Unexpected gh repo view output"
    return parsed, None


def _ensure_github_repo(name_with_owner, create_if_missing=False, visibility="private"):
    metadata, error = _github_repo_metadata(name_with_owner)
    if metadata:
        return metadata
    if not create_if_missing:
        raise RuntimeError(error or f"GitHub repository not found: {name_with_owner}")
    visibility_flag = str(visibility or "private").strip().lower()
    if visibility_flag not in ("public", "private", "internal"):
        visibility_flag = "private"
    _, create_error = _run_gh([
        "repo",
        "create",
        name_with_owner,
        f"--{visibility_flag}",
    ])
    if create_error:
        raise RuntimeError(f"Failed to create GitHub repository {name_with_owner}: {create_error}")
    metadata, error = _github_repo_metadata(name_with_owner)
    if not metadata:
        raise RuntimeError(error or f"Failed to inspect GitHub repository {name_with_owner} after creation")
    return metadata


def _sync_project_control_clone(clone_source, target: Path):
    target = target.resolve()
    desired_origin = str(clone_source or "").strip()
    if not desired_origin:
        raise RuntimeError("Repository clone source is required")
    if target.exists():
        if not (target / ".git").exists():
            _remove_path(target)
        else:
            current_origin = _git_origin_remote_url(target)
            if current_origin and current_origin != desired_origin:
                _remove_path(target)
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            ["git", "clone", desired_origin, str(target)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or f"clone failed for {desired_origin}"
            raise RuntimeError(detail)
        return
    subprocess.run(
        ["git", "-C", str(target), "remote", "set-url", "origin", desired_origin],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    fetch = subprocess.run(
        ["git", "-C", str(target), "fetch", "origin", "--prune"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if fetch.returncode != 0:
        detail = fetch.stderr.strip() or fetch.stdout.strip() or "git fetch failed"
        raise RuntimeError(f"Failed to update cached repository clone: {detail}")


def _run_git(repo_path: Path, args):
    return subprocess.run(
        ["git", "-C", str(repo_path), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )


def _git_config_value(repo_path: Path, key):
    completed = _run_git(repo_path, ["config", "--get", str(key)])
    if completed.returncode != 0:
        return None
    value = str(completed.stdout or "").strip()
    return value or None


def _git_global_config_value(key):
    completed = subprocess.run(
        ["git", "config", "--global", "--get", str(key)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return None
    value = str(completed.stdout or "").strip()
    return value or None


def _ensure_git_identity(repo_path: Path, source_repo: Path | None = None):
    user_name = _git_config_value(repo_path, "user.name")
    user_email = _git_config_value(repo_path, "user.email")
    if user_name and user_email:
        return

    source_name = _git_config_value(source_repo, "user.name") if source_repo else None
    source_email = _git_config_value(source_repo, "user.email") if source_repo else None
    fallback_name = source_name or _git_global_config_value("user.name") or "Codeswarm Router"
    fallback_email = source_email or _git_global_config_value("user.email") or "codeswarm-router@local"

    if not user_name:
        _run_git(repo_path, ["config", "user.name", fallback_name])
    if not user_email:
        _run_git(repo_path, ["config", "user.email", fallback_email])


def _git_has_remote(repo_path: Path, remote_name="origin"):
    completed = _run_git(repo_path, ["remote", "get-url", str(remote_name)])
    return completed.returncode == 0


def _git_has_commit(repo_path: Path):
    completed = _run_git(repo_path, ["rev-parse", "--verify", "HEAD"])
    return completed.returncode == 0


def _project_repo_dir(project):
    repo_path = str((project or {}).get("repo_path") or "").strip()
    if not repo_path:
        raise RuntimeError("Project repo_path is missing")
    repo_dir = Path(repo_path).expanduser().resolve()
    if not repo_dir.exists() or not (repo_dir / ".git").exists():
        raise RuntimeError(f"Project repository is unavailable: {repo_dir}")
    return repo_dir


def _refresh_project_repo_for_resume(project):
    repo_dir = _project_repo_dir(project)
    remote_url = str((project or {}).get("repo_remote_url") or "").strip()
    repo_mode = str((project or {}).get("repo_mode") or "").strip().lower()
    if repo_mode == "github" and remote_url:
        _sync_project_control_clone(remote_url, repo_dir)
        return repo_dir
    origin_url = _git_origin_remote_url(repo_dir)
    if origin_url:
        fetch = _run_git(repo_dir, ["fetch", "origin", "--prune"])
        if fetch.returncode != 0:
            detail = fetch.stderr.strip() or fetch.stdout.strip() or "git fetch failed"
            raise RuntimeError(f"Failed to refresh project repository: {detail}")
    return repo_dir


def _git_resolve_revision(repo_dir: Path, revisions):
    for revision in revisions:
        rev = str(revision or "").strip()
        if not rev:
            continue
        completed = _run_git(repo_dir, ["rev-parse", rev])
        if completed.returncode == 0:
            value = str(completed.stdout or "").strip()
            if value:
                return value
    return None


def _git_is_ancestor(repo_dir: Path, ancestor_rev, descendant_rev):
    ancestor = str(ancestor_rev or "").strip()
    descendant = str(descendant_rev or "").strip()
    if not ancestor or not descendant:
        return False
    completed = _run_git(repo_dir, ["merge-base", "--is-ancestor", ancestor, descendant])
    return completed.returncode == 0


def _resolve_project_repo_spec(
    repo_path="",
    repo_mode=None,
    github_owner=None,
    github_repo=None,
    github_create_if_missing=False,
    github_visibility="private",
):
    mode = str(repo_mode or "").strip().lower()
    owner = str(github_owner or "").strip()
    repo = str(github_repo or "").strip()
    raw_repo_path = str(repo_path or "").strip()

    if not mode:
        if owner and repo:
            mode = "github"
        else:
            mode = "local_path"

    if mode == "github":
        if not (owner and repo):
            parsed = _parse_github_repo_ref(raw_repo_path)
            if parsed:
                owner, repo = parsed.split("/", 1)
        if not (owner and repo):
            raise RuntimeError("github repo mode requires github_owner and github_repo")
        name_with_owner = f"{owner}/{repo}"
        metadata = _ensure_github_repo(
            name_with_owner,
            create_if_missing=bool(github_create_if_missing),
            visibility=github_visibility,
        )
        clone_source = str(metadata.get("sshUrl") or "").strip() or f"git@github.com:{name_with_owner}.git"
        cache_root = _project_repo_cache_root() / owner / repo
        _sync_project_control_clone(clone_source, cache_root)
        default_branch_ref = metadata.get("defaultBranchRef") or {}
        default_branch = str((default_branch_ref or {}).get("name") or "").strip() or None
        return {
            "repo_mode": "github",
            "repo_path": str(cache_root.resolve()),
            "repo_label": name_with_owner,
            "repo_remote_url": clone_source,
            "github_owner": owner,
            "github_repo": repo,
            "github_visibility": str(metadata.get("visibility") or github_visibility or "private"),
            "github_create_if_missing": bool(github_create_if_missing),
            "default_branch": default_branch,
        }

    source_path = Path(raw_repo_path).expanduser()
    if not raw_repo_path:
        raise RuntimeError("Repository path is required")
    if not source_path.exists() or not source_path.is_dir():
        raise RuntimeError(f"repo_path does not exist: {raw_repo_path}")
    resolved = source_path.resolve()
    if not (resolved / ".git").exists():
        raise RuntimeError(f"Repository path is not a git repository: {resolved}")
    remote_url = _git_origin_remote_url(resolved)
    parsed = _parse_github_repo_ref(remote_url or "")
    parsed_owner = None
    parsed_repo = None
    if parsed:
        parsed_owner, parsed_repo = parsed.split("/", 1)
    return {
        "repo_mode": "local_path",
        "repo_path": str(resolved),
        "repo_label": str(resolved),
        "repo_remote_url": remote_url,
        "github_owner": parsed_owner,
        "github_repo": parsed_repo,
        "github_visibility": None,
        "github_create_if_missing": False,
        "default_branch": None,
    }


def _project_task_branch_name(project, task):
    project_token = _sanitize_branch_token(project.get("project_id"))
    task_kind = str(task.get("task_kind") or "implementation").strip().lower()
    if task_kind == "integration":
        return f"codeswarm/{project_token}/integration"
    task_token = _sanitize_branch_token(task.get("task_id"))
    return f"codeswarm/{project_token}/{task_token}"


def _next_project_task_id(existing_ids):
    numeric_ids = []
    for task_id in existing_ids:
        match = re.fullmatch(r"T-(\d{3})", str(task_id or "").strip())
        if match:
            numeric_ids.append(int(match.group(1)))
    candidate = max(numeric_ids, default=0) + 1
    while True:
        task_id = f"T-{candidate:03d}"
        if task_id not in existing_ids:
            return task_id
        candidate += 1


def _build_integration_task(project_id, base_branch, task_order, tasks):
    dependency_ids = [task_id for task_id in task_order if task_id in tasks]
    project_stub = {
        "project_id": project_id,
        "base_branch": base_branch,
    }
    integration_task_id = _next_project_task_id(set(tasks.keys()))
    integration_task = {
        "task_id": integration_task_id,
        "title": "Integrate completed task branches",
        "prompt": "",
        "acceptance_criteria": [
            "Create or update the project integration branch from the base branch.",
            "Merge every completed task branch into the integration branch in dependency order.",
            "Run the strongest repo-level verification you can find and report what ran.",
            "Push the integration branch when an origin remote exists.",
        ],
        "depends_on": dependency_ids,
        "owned_paths": [],
        "expected_touch_paths": [],
        "task_kind": "integration",
        "system_generated": True,
    }
    branch_lines = "\n".join(
        f"- {task_id}: {_project_task_branch_name(project_stub, tasks[task_id])}"
        for task_id in dependency_ids
    ) or "- none"
    integration_branch = _project_task_branch_name(project_stub, integration_task)
    integration_task["prompt"] = (
        "This is the final integration task for the project.\n"
        f"Create or reset the integration branch `{integration_branch}` from the base branch `{base_branch}`.\n"
        "Merge the completed task branches listed below in the listed order. Fetch any source branch from `origin` first if it is not already available locally. Resolve merge conflicts carefully without rewriting the source task branches.\n"
        "After merging, run the strongest repository-level verification you can find. Prefer an existing `verify` or smoke target, otherwise run the most appropriate test/build commands available in the repo.\n"
        "If verification fails, either fix the integrated branch and rerun verification or return status `blocked` with a concise explanation.\n"
        "Do not create a pull request; the required output is the integrated branch and merge commit.\n\n"
        "Integration source branches:\n"
        f"{branch_lines}\n"
    )
    return integration_task


def _project_task_is_ready(project, task_id):
    task = (project.get("tasks") or {}).get(task_id) or {}
    if task.get("status") not in ("pending", "ready"):
        return False
    for dep_id in task.get("depends_on") or []:
        dep = (project.get("tasks") or {}).get(dep_id) or {}
        if dep.get("status") != "completed":
            return False
    candidate_paths = _project_task_paths(task)
    if not candidate_paths:
        return True
    for other_id, other_task in (project.get("tasks") or {}).items():
        if other_id == task_id or other_task.get("status") != "assigned":
            continue
        other_paths = _project_task_paths(other_task)
        if other_paths and _paths_overlap(candidate_paths, other_paths):
            return False
    return True


def _project_task_counts(project):
    counts = {
        "pending": 0,
        "ready": 0,
        "assigned": 0,
        "completed": 0,
        "failed": 0,
        "blocked": 0,
    }
    tasks = project.get("tasks") or {}
    for task_id, task in tasks.items():
        status = str(task.get("status") or "pending")
        if status == "assigned":
            counts["assigned"] += 1
        elif status == "completed":
            counts["completed"] += 1
        elif status == "failed":
            counts["failed"] += 1
        else:
            if _project_task_is_ready(project, task_id):
                counts["ready"] += 1
            else:
                counts["blocked"] += 1
            counts["pending"] += 1
    return counts


def _refresh_project_status(project):
    counts = _project_task_counts(project)
    project["task_counts"] = counts
    project["updated_at"] = time.time()
    current = str(project.get("status") or "draft")
    if current in ("draft", "starting", "resuming", "error"):
        return
    total = len(project.get("tasks") or {})
    if total > 0 and counts["completed"] == total:
        project["status"] = "completed"
    elif counts["failed"] > 0 and counts["assigned"] == 0 and counts["ready"] == 0:
        project["status"] = "attention"
    else:
        project["status"] = "running"


def _project_snapshot():
    with SCHEDULER_LOCK:
        snapshot = copy.deepcopy(PROJECTS)
    for project in snapshot.values():
        _refresh_project_status(project)
    return snapshot


def _emit_projects_updated():
    emit_event("projects_updated", {
        "projects": _project_snapshot()
    })


def _normalize_graph_from_parsed_payload(parsed):
    if isinstance(parsed, dict):
        if isinstance(parsed.get("tasks"), list):
            return parsed
        if isinstance(parsed.get("graph"), dict) and isinstance(parsed["graph"].get("tasks"), list):
            return parsed["graph"]
    if isinstance(parsed, list):
        return {"tasks": parsed}
    return None


def _beads_cli_path():
    return shutil.which("bd") or shutil.which("beads")


def _beads_env():
    env = os.environ.copy()
    home_override = str(os.environ.get("CODESWARM_BEADS_HOME") or "").strip()
    if home_override:
        env["HOME"] = home_override
    return env


def _run_beads(repo_path, args, input_text=None):
    cli = _beads_cli_path()
    if not cli:
        return None, "bd CLI not installed"
    try:
        completed = subprocess.run(
            [cli, *args],
            cwd=str(repo_path),
            input=input_text,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            env=_beads_env(),
        )
    except Exception as e:
        return None, str(e)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or f"exit {completed.returncode}").strip()
        return None, detail
    return completed, None


def _parse_json_output(text):
    if not isinstance(text, str):
        return None
    snippet = text.strip()
    if not snippet:
        return None
    for start_char in ("{", "["):
        start = snippet.find(start_char)
        if start >= 0:
            try:
                return json.loads(snippet[start:])
            except Exception:
                continue
    return None


def _beads_safe_prefix(repo_path):
    name = Path(str(repo_path or "")).name or "codeswarm"
    token = re.sub(r"[^a-zA-Z0-9]+", "-", name.lower()).strip("-")
    if not token:
        token = "codeswarm"
    if not token[0].isalpha():
        token = f"p-{token}"
    return token[:40]


def _project_beads_control_branch(project):
    project_token = _sanitize_branch_token((project or {}).get("project_id"))
    return f"{PROJECT_CONTROL_BRANCH_PREFIX}/{project_token}"


def _project_control_clone_source(project):
    remote_url = str((project or {}).get("repo_remote_url") or "").strip()
    if remote_url:
        return remote_url
    repo_path = str((project or {}).get("repo_path") or "").strip()
    if repo_path:
        origin_url = _git_origin_remote_url(Path(repo_path).expanduser().resolve())
        if origin_url:
            return origin_url
    if repo_path:
        return str(Path(repo_path).expanduser().resolve())
    raise RuntimeError("project repository source is unavailable")


def _project_control_clone_dir(project):
    project_id = str((project or {}).get("project_id") or "").strip() or "project"
    source = _project_control_clone_source(project)
    digest = hashlib.sha1(source.encode("utf-8")).hexdigest()[:12]
    return (_project_control_cache_root() / f"{project_id}-{digest}").resolve()


def _resolve_control_branch_start_ref(repo_dir: Path, branch_name, base_branch):
    return _git_resolve_revision(
        repo_dir,
        [
            f"origin/{branch_name}",
            f"refs/remotes/origin/{branch_name}",
            branch_name,
            f"refs/heads/{branch_name}",
            base_branch,
            f"origin/{base_branch}",
            f"refs/heads/{base_branch}",
            f"refs/remotes/origin/{base_branch}",
            "HEAD",
        ],
    )


def _checkout_project_control_branch(repo_dir: Path, branch_name, base_branch):
    start_ref = _resolve_control_branch_start_ref(repo_dir, branch_name, base_branch)
    if start_ref:
        completed = _run_git(repo_dir, ["checkout", "-B", branch_name, start_ref])
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or "git checkout failed"
            raise RuntimeError(f"Failed to prepare control branch {branch_name}: {detail}")
        return
    if _git_has_commit(repo_dir):
        completed = _run_git(repo_dir, ["checkout", "-B", branch_name])
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or "git checkout failed"
            raise RuntimeError(f"Failed to prepare control branch {branch_name}: {detail}")
        return
    completed = _run_git(repo_dir, ["checkout", "--orphan", branch_name])
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "git checkout failed"
        raise RuntimeError(f"Failed to create orphan control branch {branch_name}: {detail}")


def _export_project_beads_snapshot(project, output_path: Path):
    repo_dir = _project_repo_dir(project)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _, error = _run_beads(repo_dir, ["export", "-o", str(output_path)])
    if error:
        raise RuntimeError(f"Failed to export Beads issues: {error}")


def _copy_portable_beads_files(project, control_dir: Path):
    repo_dir = _project_repo_dir(project)
    source_root = repo_dir / ".beads"
    if not source_root.exists():
        return []
    copied_paths = []
    for relative_path in (
        Path(".beads/.gitignore"),
        Path(".beads/README.md"),
        Path(".beads/config.yaml"),
        Path(".beads/metadata.json"),
    ):
        source_path = repo_dir / relative_path
        if not source_path.exists() or not source_path.is_file():
            continue
        target_path = control_dir / relative_path
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, target_path)
        copied_paths.append(relative_path.as_posix())
    return copied_paths


def _persist_project_beads_snapshot(project):
    if not project or not _project_beads_available(project):
        return
    project_id = str((project or {}).get("project_id") or "").strip() or "project"
    with PROJECT_BEADS_PERSIST_LOCKS[project_id]:
        repo_dir = _project_repo_dir(project)
        control_dir = _project_control_clone_dir(project)
        _sync_project_control_clone(_project_control_clone_source(project), control_dir)
        _ensure_git_identity(control_dir, repo_dir)

        branch_name = _project_beads_control_branch(project)
        base_branch = str((project or {}).get("base_branch") or "main").strip() or "main"
        _checkout_project_control_branch(control_dir, branch_name, base_branch)

        export_path = control_dir / ".beads" / "issues.jsonl"
        stage_paths = _copy_portable_beads_files(project, control_dir)
        _export_project_beads_snapshot(project, export_path)
        stage_paths.append(".beads/issues.jsonl")
        add_result = _run_git(control_dir, ["add", "--force", *stage_paths])
        if add_result.returncode != 0:
            detail = add_result.stderr.strip() or add_result.stdout.strip() or "git add failed"
            raise RuntimeError(f"Failed to stage Beads export: {detail}")

        diff_result = _run_git(control_dir, ["diff", "--cached", "--quiet", "--", *stage_paths])
        if diff_result.returncode == 0:
            return
        if diff_result.returncode not in (0, 1):
            detail = diff_result.stderr.strip() or diff_result.stdout.strip() or "git diff failed"
            raise RuntimeError(f"Failed to inspect staged Beads export: {detail}")

        commit_message = (
            f"codeswarm: persist beads snapshot for {project.get('project_id')}\n\n"
            f"Project: {project.get('title')}\n"
            f"Branch: {branch_name}\n"
        )
        commit_result = _run_git(control_dir, ["commit", "-m", commit_message])
        if commit_result.returncode != 0:
            detail = commit_result.stderr.strip() or commit_result.stdout.strip() or "git commit failed"
            raise RuntimeError(f"Failed to commit Beads export: {detail}")

        if _git_has_remote(control_dir):
            push_result = _run_git(control_dir, ["push", "-u", "origin", branch_name])
            if push_result.returncode != 0:
                detail = push_result.stderr.strip() or push_result.stdout.strip() or "git push failed"
                raise RuntimeError(f"Failed to push Beads export: {detail}")


def _project_beads_status(project):
    status = str(project.get("beads_sync_status") or "").strip()
    return status or "disabled"


def _project_beads_available(project):
    return _project_beads_status(project) not in ("disabled", "unavailable")


def _set_project_beads_status(project, status, error=None):
    project["beads_sync_status"] = str(status)
    project["beads_last_error"] = str(error).strip() if error else None
    project["updated_at"] = time.time()


def _set_task_beads_status(task, status, error=None):
    task["beads_sync_status"] = str(status)
    task["beads_last_error"] = str(error).strip() if error else None
    task["updated_at"] = time.time()


def _ensure_beads_repo(project):
    repo_path = str(project.get("repo_path") or "").strip()
    if not repo_path:
        return None, "missing repo_path"
    repo_dir = Path(repo_path)
    if not repo_dir.exists() or not repo_dir.is_dir():
        return None, f"repo_path does not exist: {repo_path}"
    where_result, where_error = _run_beads(repo_dir, ["where", "--json"])
    if where_result:
        parsed = _parse_json_output(where_result.stdout)
        if isinstance(parsed, dict):
            return parsed, None
    init_args = [
        "init",
        "--skip-hooks",
        "--skip-agents",
        "-p",
        _beads_safe_prefix(repo_dir),
    ]
    _, init_error = _run_beads(repo_dir, init_args)
    if init_error:
        return None, init_error if where_error is None else f"{where_error}; {init_error}"
    where_result, where_error = _run_beads(repo_dir, ["where", "--json"])
    if not where_result:
        return None, where_error or "beads repo initialized but location lookup failed"
    parsed = _parse_json_output(where_result.stdout)
    if not isinstance(parsed, dict):
        return None, "unable to parse `bd where --json` output"
    return parsed, None


def _build_project_beads_description(project):
    repo_path = str(project.get("repo_path") or "").strip()
    repo_label = str(project.get("repo_label") or repo_path).strip() or repo_path
    base_branch = str(project.get("base_branch") or "main").strip() or "main"
    workspace_subdir = str(project.get("workspace_subdir") or "repo").strip() or "repo"
    return (
        "Codeswarm orchestrated project.\n\n"
        f"Project ID: {project.get('project_id')}\n"
        f"Repository: {repo_label}\n"
        f"Repository local path: {repo_path}\n"
        f"Base branch: {base_branch}\n"
        f"Worker workspace subdir: {workspace_subdir}\n"
    )


def _build_task_beads_description(task):
    prompt = str(task.get("prompt") or "").strip()
    acceptance = task.get("acceptance_criteria") or []
    depends_on = task.get("depends_on") or []
    owned_paths = task.get("owned_paths") or []
    expected_touch_paths = task.get("expected_touch_paths") or []
    sections = [
        f"Codeswarm task ID: {task.get('task_id')}",
        "",
        "Prompt:",
        prompt or "(no prompt provided)",
    ]
    if acceptance:
        sections.extend(["", "Acceptance criteria:"])
        sections.extend(f"- {item}" for item in acceptance if str(item).strip())
    if depends_on:
        sections.extend(["", "Depends on:"])
        sections.extend(f"- {item}" for item in depends_on if str(item).strip())
    if owned_paths:
        sections.extend(["", "Owned paths:"])
        sections.extend(f"- {item}" for item in owned_paths if str(item).strip())
    if expected_touch_paths:
        sections.extend(["", "Expected touch paths:"])
        sections.extend(f"- {item}" for item in expected_touch_paths if str(item).strip())
    return "\n".join(sections)


def _create_beads_issue(repo_path, title, issue_type, description, metadata, parent_id=None):
    args = [
        "create",
        "--title",
        str(title),
        "--type",
        str(issue_type),
        "--description",
        str(description),
        "--metadata",
        json.dumps(metadata, sort_keys=True),
        "--json",
    ]
    if parent_id:
        args.extend(["--parent", str(parent_id)])
    result, error = _run_beads(repo_path, args)
    if not result:
        return None, error
    parsed = _parse_json_output(result.stdout)
    if not isinstance(parsed, dict) or not parsed.get("id"):
        return None, "unable to parse `bd create --json` output"
    return parsed, None


def _sync_project_to_beads(project):
    if str(os.environ.get("CODESWARM_DISABLE_BEADS_SYNC") or "").strip().lower() in ("1", "true", "yes", "on"):
        _set_project_beads_status(project, "disabled")
        return
    cli = _beads_cli_path()
    if not cli:
        _set_project_beads_status(project, "unavailable", "bd CLI not installed")
        return

    location, location_error = _ensure_beads_repo(project)
    if location_error:
        _set_project_beads_status(project, "warning", location_error)
        return

    project["beads_repo_path"] = location.get("path")
    project["beads_prefix"] = location.get("prefix")
    project["beads_db_path"] = location.get("database_path")

    root_id = str(project.get("beads_root_id") or "").strip()
    if not root_id:
        created, create_error = _create_beads_issue(
            project.get("repo_path"),
            project.get("title") or f"Project {project.get('project_id')}",
            "epic",
            _build_project_beads_description(project),
            {
                "codeswarm_project_id": project.get("project_id"),
                "repo_path": project.get("repo_path"),
                "base_branch": project.get("base_branch"),
                "workspace_subdir": project.get("workspace_subdir"),
            },
        )
        if create_error:
            _set_project_beads_status(project, "warning", create_error)
            return
        root_id = str(created.get("id") or "").strip()
        project["beads_root_id"] = root_id

    task_errors = []
    tasks = project.get("tasks") or {}
    for task_id in project.get("task_order") or list(tasks.keys()):
        task = tasks.get(task_id)
        if not isinstance(task, dict):
            continue
        beads_id = str(task.get("beads_id") or "").strip()
        if not beads_id:
            created, create_error = _create_beads_issue(
                project.get("repo_path"),
                task.get("title") or task_id,
                "task",
                _build_task_beads_description(task),
                {
                    "codeswarm_project_id": project.get("project_id"),
                    "codeswarm_task_id": task_id,
                    "base_branch": project.get("base_branch"),
                },
                parent_id=root_id or None,
            )
            if create_error:
                task_errors.append(f"{task_id}: {create_error}")
                _set_task_beads_status(task, "warning", create_error)
                continue
            beads_id = str(created.get("id") or "").strip()
            task["beads_id"] = beads_id
        synced_deps = set()
        raw_synced_deps = task.get("beads_dependencies_synced")
        if isinstance(raw_synced_deps, list):
            synced_deps = {str(item) for item in raw_synced_deps if str(item).strip()}
        for dep_id in task.get("depends_on") or []:
            blocker = tasks.get(dep_id) or {}
            blocker_beads_id = str(blocker.get("beads_id") or "").strip()
            if not blocker_beads_id or not beads_id or dep_id in synced_deps:
                continue
            _, dep_error = _run_beads(project.get("repo_path"), ["dep", blocker_beads_id, "--blocks", beads_id])
            if dep_error:
                task_errors.append(f"{task_id}: {dep_error}")
                _set_task_beads_status(task, "warning", dep_error)
                continue
            synced_deps.add(str(dep_id))
        task["beads_dependencies_synced"] = sorted(synced_deps)
        if task.get("beads_id"):
            _set_task_beads_status(task, "synced")

    if task_errors:
        _set_project_beads_status(project, "partial", "; ".join(task_errors[:3]))
    else:
        _set_project_beads_status(project, "synced")
    try:
        _persist_project_beads_snapshot(project)
    except Exception as e:
        _set_project_beads_status(project, "warning", str(e))


def _sync_task_status_to_beads(project, task):
    if not project or not task or not _project_beads_available(project):
        return
    beads_id = str(task.get("beads_id") or "").strip()
    if not beads_id:
        return
    repo_path = project.get("repo_path")
    task_status = str(task.get("status") or "").strip().lower()
    result_status = str(task.get("result_status") or "").strip().lower()
    branch = str(task.get("branch") or "").strip()

    if task_status == "assigned":
        _, error = _run_beads(repo_path, ["update", beads_id, "--status", "in_progress"])
        if error:
            _set_task_beads_status(task, "warning", error)
        else:
            _set_task_beads_status(task, "synced")
            try:
                _persist_project_beads_snapshot(project)
            except Exception as e:
                _set_project_beads_status(project, "warning", str(e))
        return

    if task_status == "completed":
        reason = f"Completed by Codeswarm on branch {branch or 'n/a'}"
        _, error = _run_beads(repo_path, ["close", beads_id, "--reason", reason])
        if error:
            _set_task_beads_status(task, "warning", error)
        else:
            _set_task_beads_status(task, "closed")
            try:
                _persist_project_beads_snapshot(project)
            except Exception as e:
                _set_project_beads_status(project, "warning", str(e))
        return

    if task_status == "failed":
        note = str(task.get("last_error") or result_status or "Task requires attention").strip()
        _, error = _run_beads(repo_path, ["update", beads_id, "--status", "blocked", "--append-notes", note])
        if error:
            _set_task_beads_status(task, "warning", error)
        else:
            _set_task_beads_status(task, "synced")
            try:
                _persist_project_beads_snapshot(project)
            except Exception as e:
                _set_project_beads_status(project, "warning", str(e))


def _parse_task_graph_json_block(text):
    if not isinstance(text, str):
        return None
    marker = "TASK_GRAPH_JSON"
    marker_positions = [match.start() for match in re.finditer(re.escape(marker), text)]
    if not marker_positions:
        return None

    best_graph = None
    for idx, marker_idx in enumerate(marker_positions):
        next_idx = marker_positions[idx + 1] if idx + 1 < len(marker_positions) else len(text)
        snippet = text[marker_idx + len(marker):next_idx].strip()
        if not snippet:
            continue

        candidates = []
        fence_matches = list(re.finditer(r"```json\s*([\s\S]+?)```", snippet, re.IGNORECASE))
        for fence_match in fence_matches:
            fenced = fence_match.group(1).strip()
            if fenced:
                candidates.append(fenced)
        if not candidates:
            candidates.append(snippet)

        for raw_json in candidates:
            try:
                parsed = json.loads(raw_json)
            except Exception:
                continue
            graph = _normalize_graph_from_parsed_payload(parsed)
            if not graph:
                continue
            tasks = graph.get("tasks")
            if isinstance(tasks, list) and tasks:
                best_graph = graph
            elif best_graph is None:
                best_graph = graph

    return best_graph


def _create_project_record(
    title,
    repo_path,
    worker_swarm_ids,
    raw_tasks,
    base_branch="main",
    workspace_subdir="repo",
    repo_meta=None,
):
    if not title or not repo_path:
        raise RuntimeError("project requires title and repo_path")
    if not isinstance(worker_swarm_ids, list) or not worker_swarm_ids:
        raise RuntimeError("project requires worker_swarm_ids")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise RuntimeError("project requires at least one task")

    normalized_swarm_ids = []
    missing_swarms = []
    for item in worker_swarm_ids:
        swarm_id = str(item)
        if swarm_id not in SWARMS:
            missing_swarms.append(swarm_id)
        else:
            normalized_swarm_ids.append(swarm_id)
    if missing_swarms:
        raise RuntimeError(f"unknown worker_swarm_ids: {', '.join(missing_swarms)}")

    project_id = str(uuid.uuid4())
    tasks = {}
    task_order = []
    for idx, raw_task in enumerate(raw_tasks):
        task = _normalize_task_payload(raw_task, idx)
        task_id = task["task_id"]
        if task_id in tasks:
            raise RuntimeError(f"Duplicate task_id: {task_id}")
        tasks[task_id] = task
        task_order.append(task_id)
    has_integration_task = any(
        str(task.get("task_kind") or "").strip().lower() == "integration"
        for task in tasks.values()
    )
    if not has_integration_task:
        integration_task = _normalize_task_payload(
            _build_integration_task(project_id, str(base_branch or "main"), task_order, tasks),
            len(task_order),
        )
        tasks[integration_task["task_id"]] = integration_task
        task_order.append(integration_task["task_id"])
    for task_id, task in tasks.items():
        missing = [dep_id for dep_id in task.get("depends_on") or [] if dep_id not in tasks]
        if missing:
            raise RuntimeError(f"Task {task_id} references unknown dependencies: {', '.join(missing)}")

    now = time.time()
    project = {
        "project_id": project_id,
        "title": str(title),
        "repo_path": str(repo_path),
        "repo_mode": str((repo_meta or {}).get("repo_mode") or "local_path"),
        "repo_label": str((repo_meta or {}).get("repo_label") or repo_path),
        "repo_remote_url": (repo_meta or {}).get("repo_remote_url"),
        "github_owner": (repo_meta or {}).get("github_owner"),
        "github_repo": (repo_meta or {}).get("github_repo"),
        "github_visibility": (repo_meta or {}).get("github_visibility"),
        "github_create_if_missing": bool((repo_meta or {}).get("github_create_if_missing")),
        "base_branch": str(base_branch or "main"),
        "worker_swarm_ids": normalized_swarm_ids,
        "status": "draft",
        "workspace_subdir": str(workspace_subdir or "repo"),
        "repo_preparation": {},
        "task_order": task_order,
        "tasks": tasks,
        "beads_sync_status": "pending",
        "beads_last_error": None,
        "beads_repo_path": None,
        "beads_prefix": None,
        "beads_db_path": None,
        "beads_root_id": None,
        "integration_branch": None,
        "integration_head_commit": None,
        "final_result_branch": None,
        "final_result_head_commit": None,
        "resume_count": 0,
        "last_resume_at": None,
        "last_resumed_by_worker_swarm_ids": [],
        "resume_summary": None,
        "usage": _empty_usage_totals(),
        "usage_updated_at": None,
        "worker_usage": {},
        "created_at": now,
        "updated_at": now,
    }
    _refresh_project_status(project)
    _sync_project_to_beads(project)
    with SCHEDULER_LOCK:
        PROJECTS[project_id] = project
    save_state()
    _emit_projects_updated()
    return project


def _build_project_plan_prompt(title, repo_path, spec_text, base_branch, workspace_subdir="repo", repo_label=None, repo_remote_url=None):
    display_repo = str(repo_label or repo_path or "").strip() or str(repo_path or "").strip()
    remote_url = str(repo_remote_url or "").strip()
    repo_workspace = str(workspace_subdir or "repo").strip() or "repo"
    return (
        "You are the planning swarm for an orchestrated project.\n"
        "Decompose the specification into implementation-ready tasks for the referenced repository.\n"
        "Do not include the final merge/integration task yourself; Codeswarm appends that automatically after planning.\n"
        "A clone of the repository is available in your workspace for inspection before planning.\n"
        "Each task must include task_id, title, prompt, acceptance_criteria, depends_on, and optional owned_paths.\n"
        "Do not return an empty task list.\n"
        "If the specification requires an exact task count, produce exactly that many tasks.\n"
        "Do not use generic graph schemas such as `nodes` and `edges`.\n"
        "Your final answer must begin with `TASK_GRAPH_JSON` on its own line.\n"
        "The JSON top-level object must contain a `tasks` array.\n\n"
        f"Project title: {title}\n"
        f"Repository reference: {display_repo}\n"
        f"Repository workspace: ./{repo_workspace}\n"
        + (f"Repository remote: {remote_url}\n" if remote_url else "")
        + f"Repository local path: {repo_path}\n"
        f"Base branch: {base_branch or 'main'}\n\n"
        "Specification:\n"
        f"{spec_text}\n\n"
        "Return the task graph as JSON using this exact header and a JSON code fence:\n"
        "TASK_GRAPH_JSON\n"
        "```json\n"
        "{\n"
        "  \"tasks\": [\n"
        "    {\n"
        "      \"task_id\": \"T-001\",\n"
        "      \"title\": \"...\",\n"
        "      \"prompt\": \"...\",\n"
        "      \"acceptance_criteria\": [\"...\"],\n"
        "      \"depends_on\": [],\n"
        "      \"owned_paths\": [\"optional/path\"]\n"
        "    }\n"
        "  ]\n"
        "}\n"
        "```\n"
    )


def _record_project_plan_result(plan_id, data):
    with SCHEDULER_LOCK:
        plan = PENDING_PROJECT_PLANS.get(str(plan_id))
    if not plan:
        return
    raw_text = _extract_text_content(data.get("last_agent_message")) or ""
    graph = _parse_task_graph_json_block(raw_text)
    if not graph or not isinstance(graph.get("tasks"), list):
        with SCHEDULER_LOCK:
            current = PENDING_PROJECT_PLANS.get(str(plan_id))
            if current:
                current["status"] = "failed"
                current["last_error"] = "TASK_GRAPH_JSON block missing or malformed"
                current["updated_at"] = time.time()
        emit_event("project_plan_failed", {
            "plan_id": plan_id,
            "reason": "TASK_GRAPH_JSON block missing or malformed",
        })
        save_state()
        return
    if not graph.get("tasks"):
        with SCHEDULER_LOCK:
            current = PENDING_PROJECT_PLANS.get(str(plan_id))
            if current:
                current["status"] = "failed"
                current["last_error"] = "TASK_GRAPH_JSON must contain at least one task"
                current["updated_at"] = time.time()
        emit_event("project_plan_failed", {
            "plan_id": plan_id,
            "reason": "TASK_GRAPH_JSON must contain at least one task",
        })
        save_state()
        return
    try:
        project = _create_project_record(
            plan.get("title"),
            plan.get("repo_path"),
            plan.get("worker_swarm_ids") or [],
            graph.get("tasks") or [],
            base_branch=plan.get("base_branch") or "main",
            workspace_subdir=plan.get("workspace_subdir") or "repo",
            repo_meta=plan,
        )
        emit_event("project_created", {
            "request_id": plan.get("request_id"),
            "project_id": project.get("project_id"),
            "title": project.get("title"),
        })
        emit_event("project_plan_completed", {
            "plan_id": plan_id,
            "project_id": project.get("project_id"),
        })
        auto_start = bool(plan.get("auto_start"))
        with SCHEDULER_LOCK:
            PENDING_PROJECT_PLANS.pop(str(plan_id), None)
        save_state()
        if auto_start:
            with SCHEDULER_LOCK:
                project_ref = PROJECTS.get(str(project.get("project_id")))
                if project_ref:
                    project_ref["status"] = "starting"
                    project_ref["updated_at"] = time.time()
            _emit_projects_updated()
            save_state()
            _start_project_async(project.get("project_id"), plan.get("request_id") or str(uuid.uuid4()))
    except Exception as e:
        with SCHEDULER_LOCK:
            current = PENDING_PROJECT_PLANS.get(str(plan_id))
            if current:
                current["status"] = "failed"
                current["last_error"] = str(e)
                current["updated_at"] = time.time()
        emit_event("project_plan_failed", {
            "plan_id": plan_id,
            "reason": str(e),
        })
        save_state()


def _dispatch_pending_project_plans(config):
    with SCHEDULER_LOCK:
        pending_ids = [
            plan_id
            for plan_id, plan in PENDING_PROJECT_PLANS.items()
            if isinstance(plan, dict) and str(plan.get("status") or "").strip().lower() == "queued"
        ]

    for plan_id in pending_ids:
        with SCHEDULER_LOCK:
            plan = PENDING_PROJECT_PLANS.get(str(plan_id))
            if not isinstance(plan, dict) or str(plan.get("status") or "").strip().lower() != "queued":
                continue
        planner_swarm_id = str(plan.get("planner_swarm_id") or "").strip()
        planner_swarm = SWARMS.get(planner_swarm_id)
        if not planner_swarm:
            with SCHEDULER_LOCK:
                current = PENDING_PROJECT_PLANS.get(str(plan_id))
                if current:
                    current["status"] = "failed"
                    current["last_error"] = "unknown planner_swarm_id"
                    current["updated_at"] = time.time()
            emit_event("project_plan_failed", {
                "plan_id": plan_id,
                "reason": "unknown planner_swarm_id",
            })
            save_state()
            continue
        planner_provider = _provider_for_swarm(planner_swarm_id)
        if not planner_provider:
            continue
        planner_node_id = _first_quiescent_node_id(planner_swarm_id)
        if planner_node_id is None:
            continue

        prompt = str(plan.get("prompt") or "").strip()
        if not prompt:
            prompt = _build_project_plan_prompt(
                plan.get("title"),
                plan.get("repo_path"),
                plan.get("spec"),
                plan.get("base_branch") or "main",
                plan.get("workspace_subdir") or "repo",
                plan.get("repo_label"),
                plan.get("repo_remote_url"),
            )

        try:
            planner_preparation = planner_provider.prepare_repository(
                str(planner_swarm.get("job_id")),
                str(plan.get("repo_path")),
                branch=plan.get("base_branch") or "main",
                subdir=plan.get("workspace_subdir") or "repo",
            )
        except Exception as e:
            with SCHEDULER_LOCK:
                current = PENDING_PROJECT_PLANS.get(str(plan_id))
                if current:
                    current["status"] = "failed"
                    current["last_error"] = str(e)
                    current["updated_at"] = time.time()
            emit_event("project_plan_failed", {
                "plan_id": plan_id,
                "reason": str(e),
            })
            save_state()
            continue

        _mark_outstanding(planner_swarm_id, planner_node_id, +1)
        success, injection_id, error = perform_injection(
            config,
            planner_provider,
            plan.get("request_id") or str(uuid.uuid4()),
            planner_swarm_id,
            str(planner_swarm.get("job_id")),
            int(planner_node_id),
            prompt,
            count_outstanding=False,
        )
        if not success:
            _mark_outstanding(planner_swarm_id, planner_node_id, -1)
            with SCHEDULER_LOCK:
                current = PENDING_PROJECT_PLANS.get(str(plan_id))
                if current:
                    current["status"] = "failed"
                    current["last_error"] = error or "planner injection failed"
                    current["updated_at"] = time.time()
            emit_event("project_plan_failed", {
                "plan_id": plan_id,
                "reason": error or "planner injection failed",
            })
            save_state()
            continue

        with SCHEDULER_LOCK:
            current = PENDING_PROJECT_PLANS.get(str(plan_id))
            if current:
                current["status"] = "planning"
                current["planner_node_id"] = planner_node_id
                current["injection_id"] = injection_id
                current["planner_repo_preparation"] = planner_preparation
                current["updated_at"] = time.time()
                current["prompt"] = prompt
        save_state()
        emit_event("project_plan_started", {
            "request_id": plan.get("request_id"),
            "plan_id": plan_id,
            "planner_swarm_id": planner_swarm_id,
            "planner_node_id": planner_node_id,
            "injection_id": injection_id,
        })


def _normalize_task_payload(raw_task, index):
    if not isinstance(raw_task, dict):
        raise RuntimeError(f"Task #{index + 1} must be an object")
    task_id = raw_task.get("task_id") or raw_task.get("id") or f"T-{index + 1:03d}"
    title = str(raw_task.get("title") or "").strip()
    prompt = str(raw_task.get("prompt") or "").strip()
    if not title:
        raise RuntimeError(f"Task {task_id} is missing title")
    if not prompt:
        raise RuntimeError(f"Task {task_id} is missing prompt")
    acceptance = raw_task.get("acceptance_criteria")
    depends_on = raw_task.get("depends_on")
    owned_paths = raw_task.get("owned_paths")
    expected_touch_paths = raw_task.get("expected_touch_paths")
    now = time.time()
    return {
        "task_id": str(task_id),
        "title": title,
        "prompt": prompt,
        "acceptance_criteria": acceptance if isinstance(acceptance, list) else [],
        "depends_on": [str(item) for item in (depends_on if isinstance(depends_on, list) else [])],
        "owned_paths": [str(item) for item in (owned_paths if isinstance(owned_paths, list) else [])],
        "expected_touch_paths": [str(item) for item in (expected_touch_paths if isinstance(expected_touch_paths, list) else [])],
        "task_kind": str(raw_task.get("task_kind") or raw_task.get("kind") or "implementation"),
        "system_generated": bool(raw_task.get("system_generated", False)),
        "status": "pending",
        "attempts": 0,
        "assigned_swarm_id": None,
        "assigned_node_id": None,
        "last_assigned_swarm_id": None,
        "last_assigned_node_id": None,
        "assignment_injection_id": None,
        "branch": None,
        "base_commit": None,
        "head_commit": None,
        "verified_branch_commit": None,
        "verified_at": None,
        "resume_decision": None,
        "last_resume_reason": None,
        "result_status": None,
        "result_raw": None,
        "last_error": None,
        "beads_id": None,
        "beads_sync_status": "pending",
        "beads_last_error": None,
        "beads_dependencies_synced": [],
        "usage": _empty_usage_totals(),
        "active_attempt_usage": None,
        "usage_updated_at": None,
        "created_at": now,
        "updated_at": now,
    }


def _extract_text_content(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("text", "content"):
            nested = value.get(key)
            if isinstance(nested, str):
                return nested
        parts = []
        for item in value.values():
            text = _extract_text_content(item)
            if text:
                parts.append(text)
        return "\n".join(part for part in parts if part)
    if isinstance(value, list):
        parts = []
        for item in value:
            text = _extract_text_content(item)
            if text:
                parts.append(text)
        return "\n".join(part for part in parts if part)
    return ""


def _parse_task_result_block(text):
    if not isinstance(text, str):
        return None
    lines = text.splitlines()
    start = None
    for idx, line in enumerate(lines):
        if line.strip() == "TASK_RESULT":
            start = idx + 1
            break
    if start is None:
        return None
    parsed = {}
    current_key = None
    for raw_line in lines[start:]:
        line = raw_line.rstrip()
        if not line.strip():
            continue
        if re.match(r"^[A-Z_]+$", line.strip()):
            break
        if line.lstrip().startswith("- ") and current_key:
            parsed.setdefault(current_key, [])
            parsed[current_key].append(line.lstrip()[2:].strip())
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        current_key = key.strip().lower().replace("-", "_")
        value = value.strip()
        if current_key in ("files_changed", "verification"):
            parsed[current_key] = [value] if value else []
        else:
            parsed[current_key] = value
    return parsed if parsed else None


def _build_project_task_prompt(project, task):
    branch_name = _project_task_branch_name(project, task)
    acceptance_lines = "\n".join(
        f"- {item}" for item in (task.get("acceptance_criteria") or []) if str(item).strip()
    ) or "- Satisfy the task prompt and describe any verification you ran."
    dependency_lines = "\n".join(
        f"- {item}" for item in (task.get("depends_on") or []) if str(item).strip()
    ) or "- none"
    repo_subdir = str(project.get("workspace_subdir") or "repo").strip() or "repo"
    prompt = str(task.get("prompt") or "").strip()
    return (
        f"You are executing orchestrated project task {task.get('task_id')}.\n\n"
        f"Project: {project.get('title')}\n"
        "Repository workspace: your current working directory is already the prepared repository root.\n"
        f"Prepared checkout label: ./{repo_subdir}\n"
        f"Base branch: {project.get('base_branch') or 'current checkout'}\n"
        f"Working branch to create/use: {branch_name}\n\n"
        f"Task title: {task.get('title')}\n"
        f"Task prompt:\n{prompt}\n\n"
        f"Dependencies:\n{dependency_lines}\n\n"
        f"Acceptance criteria:\n{acceptance_lines}\n\n"
        "Operate only on this task's scope. If you need to run commands or edit files, work in the current working directory unless a command explicitly requires another path.\n"
        f"Use or create the branch `{branch_name}` in the repository workspace before making changes.\n"
        "Commit your changes on that branch. If git user identity is missing, configure repository-local user.name and user.email first.\n"
        "If the repository has an origin remote, push the branch with upstream tracking before you finish.\n"
        "Your final answer must be the TASK_RESULT block only, with no extra prose before or after it.\n"
        "Return a structured result using this exact header and keys:\n\n"
        "TASK_RESULT\n"
        f"task_id: {task.get('task_id')}\n"
        "status: done|blocked|failed|needs_followups\n"
        f"branch: {branch_name}\n"
        "base_commit: <commit>\n"
        "head_commit: <commit>\n"
        "files_changed:\n"
        "- <path>\n"
        "verification:\n"
        "- <command or check>\n"
        "notes: <short summary>\n"
    )


def _find_project_and_task_by_injection(injection_id):
    if not isinstance(injection_id, str) or not injection_id:
        return None, None
    with SCHEDULER_LOCK:
        for project_id, project in PROJECTS.items():
            for task_id, task in (project.get("tasks") or {}).items():
                if task.get("assignment_injection_id") == injection_id:
                    return project_id, task_id
    return None, None


def _find_pending_project_plan_by_injection(injection_id):
    if not isinstance(injection_id, str) or not injection_id:
        return None
    with SCHEDULER_LOCK:
        for plan_id, plan in PENDING_PROJECT_PLANS.items():
            if isinstance(plan, dict) and plan.get("injection_id") == injection_id:
                return plan_id
    return None


def _project_first_idle_target(project):
    worker_swarm_ids = project.get("worker_swarm_ids") or []
    for swarm_id in worker_swarm_ids:
        swarm = SWARMS.get(str(swarm_id))
        if not swarm or swarm.get("status") in ("terminating", "terminated"):
            continue
        node_id = _first_idle_node_id(str(swarm_id))
        if node_id is not None:
            return str(swarm_id), int(node_id)
    return None, None


def _update_project_usage_for_injection(payload):
    if not isinstance(payload, dict):
        return False
    project_id, task_id = _find_project_and_task_by_injection(payload.get("injection_id"))
    if not project_id or not task_id:
        return False

    changed = False
    with SCHEDULER_LOCK:
        project = PROJECTS.get(str(project_id))
        if not isinstance(project, dict):
            return False
        task = (project.get("tasks") or {}).get(str(task_id))
        if not isinstance(task, dict):
            return False

        current_snapshot = _normalize_usage_snapshot(payload)
        if not current_snapshot:
            return False
        previous_snapshot = task.get("active_attempt_usage")
        delta = _usage_delta_for_project_accounting(current_snapshot, previous_snapshot, payload)
        task["active_attempt_usage"] = current_snapshot
        if not delta:
            return False

        task_changed = _apply_usage_delta(task, delta)
        if task_changed:
            task["usage_updated_at"] = time.time()
            changed = True

        if _apply_usage_delta(project, delta):
            project["usage_updated_at"] = time.time()
            changed = True

        swarm_id = str(payload.get("swarm_id") or task.get("assigned_swarm_id") or "").strip()
        node_id = payload.get("node_id")
        if swarm_id and isinstance(node_id, int):
            worker_usage = project.get("worker_usage")
            if not isinstance(worker_usage, dict):
                worker_usage = {}
                project["worker_usage"] = worker_usage
            worker_key = f"{swarm_id}:{node_id}"
            worker_record = worker_usage.get(worker_key)
            if not isinstance(worker_record, dict):
                worker_record = {
                    "swarm_id": swarm_id,
                    "node_id": int(node_id),
                }
                worker_usage[worker_key] = worker_record
            swarm = SWARMS.get(swarm_id)
            alias = str((swarm or {}).get("alias") or "").strip()
            if alias:
                worker_record["swarm_alias"] = alias
            if _apply_usage_delta(worker_record, delta):
                worker_record["updated_at"] = time.time()
                changed = True

    if changed:
        _emit_projects_updated()
    return changed


def _record_project_task_result(project_id, task_id, data):
    project_ref = None
    task_ref = None
    with SCHEDULER_LOCK:
        project = PROJECTS.get(str(project_id))
        if not project:
            return
        task = (project.get("tasks") or {}).get(str(task_id))
        if not task:
            return
        raw_text = _extract_text_content(data.get("last_agent_message")) or ""
        parsed = _parse_task_result_block(raw_text)
        task["result_raw"] = raw_text
        task["updated_at"] = time.time()
        task["assigned_swarm_id"] = None
        task["assigned_node_id"] = None
        task["assignment_injection_id"] = None
        task["active_attempt_usage"] = None
        task["branch"] = parsed.get("branch") if isinstance(parsed, dict) else task.get("branch")
        task["base_commit"] = parsed.get("base_commit") if isinstance(parsed, dict) else task.get("base_commit")
        task["head_commit"] = parsed.get("head_commit") if isinstance(parsed, dict) else task.get("head_commit")
        if not parsed:
            task["status"] = "failed"
            task["result_status"] = "invalid_result"
            task["last_error"] = "TASK_RESULT block missing or malformed"
        else:
            result_status = str(parsed.get("status") or "").strip().lower()
            task["result_status"] = result_status or "unknown"
            if result_status == "done":
                task["status"] = "completed"
                task["last_error"] = None
            elif result_status in ("blocked", "failed", "needs_followups"):
                task["status"] = "failed"
                task["last_error"] = parsed.get("notes") or result_status
            else:
                task["status"] = "failed"
                task["last_error"] = "Unrecognized TASK_RESULT status"
            if (
                task["status"] == "completed"
                and str(task.get("task_kind") or "").strip().lower() == "integration"
            ):
                integration_branch = str(parsed.get("branch") or task.get("branch") or "").strip() or None
                integration_head_commit = str(parsed.get("head_commit") or "").strip() or None
                task["verified_branch_commit"] = integration_head_commit
                task["verified_at"] = time.time()
                project["integration_branch"] = integration_branch
                project["integration_head_commit"] = integration_head_commit
                project["final_result_branch"] = integration_branch
                project["final_result_head_commit"] = integration_head_commit
        _refresh_project_status(project)
        project_ref = project
        task_ref = task
    _sync_task_status_to_beads(project_ref, task_ref)
    _emit_projects_updated()
    save_state()


def _task_recorded_head_commit(task):
    head_commit = str((task or {}).get("head_commit") or "").strip()
    if head_commit:
        return head_commit
    parsed = _parse_task_result_block(str((task or {}).get("result_raw") or ""))
    if not isinstance(parsed, dict):
        return None
    value = str(parsed.get("head_commit") or "").strip()
    return value or None


def _task_is_integration(task):
    return str((task or {}).get("task_kind") or "").strip().lower() == "integration"


def _reset_project_integration_result(project):
    if not isinstance(project, dict):
        return
    project["integration_branch"] = None
    project["integration_head_commit"] = None
    project["final_result_branch"] = None
    project["final_result_head_commit"] = None


def _verify_task_branch_state(project, task, repo_dir: Path):
    branch = str((task or {}).get("branch") or "").strip()
    if not branch:
        return {
            "branch": None,
            "branch_tip": None,
            "recoverable": False,
            "reason": "No task branch recorded",
        }
    branch_tip = _git_resolve_revision(
        repo_dir,
        [
            branch,
            f"refs/heads/{branch}",
            f"origin/{branch}",
            f"refs/remotes/origin/{branch}",
        ],
    )
    if not branch_tip:
        return {
            "branch": branch,
            "branch_tip": None,
            "recoverable": False,
            "reason": f"Task branch {branch} was not found",
        }
    recorded_head = _task_recorded_head_commit(task)
    if recorded_head and _git_is_ancestor(repo_dir, recorded_head, branch_tip):
        return {
            "branch": branch,
            "branch_tip": branch_tip,
            "recoverable": True,
            "reason": f"Recorded head commit is reachable from {branch}",
        }
    base_branch = str((project or {}).get("base_branch") or "main").strip() or "main"
    base_tip = _git_resolve_revision(
        repo_dir,
        [
            base_branch,
            f"refs/heads/{base_branch}",
            f"origin/{base_branch}",
            f"refs/remotes/origin/{base_branch}",
            "HEAD",
        ],
    )
    if base_tip and base_tip != branch_tip:
        return {
            "branch": branch,
            "branch_tip": branch_tip,
            "recoverable": True,
            "reason": f"Task branch {branch} diverges from base branch {base_branch}",
        }
    return {
        "branch": branch,
        "branch_tip": branch_tip,
        "recoverable": False,
        "reason": f"Task branch {branch} does not contain durable progress beyond {base_branch}",
    }


def _normalize_resume_worker_swarm_ids(worker_swarm_ids):
    if worker_swarm_ids is None:
        return None
    if not isinstance(worker_swarm_ids, list) or not worker_swarm_ids:
        raise RuntimeError("project_resume requires at least one worker_swarm_id when overriding workers")
    normalized = []
    missing = []
    for item in worker_swarm_ids:
        swarm_id = str(item or "").strip()
        if not swarm_id:
            continue
        if swarm_id not in SWARMS:
            missing.append(swarm_id)
            continue
        if swarm_id not in normalized:
            normalized.append(swarm_id)
    if missing:
        raise RuntimeError(f"unknown worker_swarm_ids: {', '.join(missing)}")
    if not normalized:
        raise RuntimeError("project_resume requires at least one valid worker_swarm_id")
    return normalized


def _project_has_live_assignments(project):
    return len(_project_live_assignment_details(project)) > 0


def _project_live_assignment_details(project):
    results = []
    tasks = (project or {}).get("tasks") or {}
    for task_id, task in tasks.items():
        if not isinstance(task, dict) or task.get("status") != "assigned":
            continue
        swarm_id = str(task.get("assigned_swarm_id") or "").strip()
        if not swarm_id:
            continue
        swarm = SWARMS.get(swarm_id)
        if swarm and swarm.get("status") not in ("terminating", "terminated"):
            results.append({
                "task_id": str(task_id),
                "title": str(task.get("title") or task_id),
                "swarm_id": swarm_id,
                "swarm_alias": str(swarm.get("alias") or swarm_id),
                "swarm_status": str(swarm.get("status") or "running"),
                "node_id": task.get("assigned_node_id"),
                "branch": task.get("branch"),
            })
    return results


def _reconcile_project_for_resume(project, retry_failed=False, reverify_completed=True):
    if not isinstance(project, dict):
        raise RuntimeError("project not found")
    tasks = project.get("tasks") or {}
    repo_dir = None
    if reverify_completed:
        repo_dir = _refresh_project_repo_for_resume(project)

    summary = {
        "kept_completed": 0,
        "recovered_from_branch": 0,
        "downgraded_to_pending": 0,
        "reset_assigned": 0,
        "retried_failed": 0,
    }
    changed_task_ids = set()
    now = time.time()

    for task_id in project.get("task_order") or list(tasks.keys()):
        task = tasks.get(task_id)
        if not isinstance(task, dict):
            continue
        status = str(task.get("status") or "").strip().lower()
        task["resume_decision"] = None
        task["last_resume_reason"] = None
        if status == "completed" and reverify_completed:
            verification = _verify_task_branch_state(project, task, repo_dir)
            if verification.get("recoverable"):
                task["verified_branch_commit"] = verification.get("branch_tip")
                task["verified_at"] = now
                task["resume_decision"] = "kept_completed"
                task["last_resume_reason"] = verification.get("reason")
                summary["kept_completed"] += 1
            else:
                task["status"] = "pending"
                task["assigned_swarm_id"] = None
                task["assigned_node_id"] = None
                task["assignment_injection_id"] = None
                task["active_attempt_usage"] = None
                task["verified_branch_commit"] = None
                task["verified_at"] = None
                task["resume_decision"] = "downgraded_to_pending"
                task["last_resume_reason"] = verification.get("reason")
                task["updated_at"] = now
                changed_task_ids.add(str(task_id))
                summary["downgraded_to_pending"] += 1
                if _task_is_integration(task):
                    _reset_project_integration_result(project)
        elif status == "assigned":
            verification = _verify_task_branch_state(project, task, repo_dir) if reverify_completed else {
                "recoverable": False,
                "reason": "Assigned task reset for resume",
                "branch_tip": None,
            }
            task["assigned_swarm_id"] = None
            task["assigned_node_id"] = None
            task["assignment_injection_id"] = None
            task["active_attempt_usage"] = None
            if verification.get("recoverable"):
                task["status"] = "completed"
                task["result_status"] = str(task.get("result_status") or "recovered_from_branch")
                task["verified_branch_commit"] = verification.get("branch_tip")
                task["head_commit"] = task.get("head_commit") or verification.get("branch_tip")
                task["verified_at"] = now
                task["resume_decision"] = "recovered_from_branch"
                task["last_resume_reason"] = verification.get("reason")
                summary["recovered_from_branch"] += 1
            else:
                task["status"] = "pending"
                task["resume_decision"] = "reset_assigned"
                task["last_resume_reason"] = verification.get("reason")
                summary["reset_assigned"] += 1
            task["updated_at"] = now
            changed_task_ids.add(str(task_id))
            if _task_is_integration(task) and task.get("status") != "completed":
                _reset_project_integration_result(project)
        elif status == "failed" and retry_failed:
            task["status"] = "pending"
            task["assigned_swarm_id"] = None
            task["assigned_node_id"] = None
            task["assignment_injection_id"] = None
            task["active_attempt_usage"] = None
            task["last_error"] = None
            task["resume_decision"] = "retried_failed"
            task["last_resume_reason"] = "Failed task reset to pending during resume"
            task["updated_at"] = now
            changed_task_ids.add(str(task_id))
            summary["retried_failed"] += 1
            if _task_is_integration(task):
                _reset_project_integration_result(project)

    changed = True
    while changed:
        changed = False
        for task_id in reversed(project.get("task_order") or list(tasks.keys())):
            task = tasks.get(task_id)
            if not isinstance(task, dict) or task.get("status") != "completed":
                continue
            missing_dep = next(
                (
                    dep_id
                    for dep_id in task.get("depends_on") or []
                    if str((tasks.get(dep_id) or {}).get("status") or "").strip().lower() != "completed"
                ),
                None,
            )
            if not missing_dep:
                continue
            task["status"] = "pending"
            task["assigned_swarm_id"] = None
            task["assigned_node_id"] = None
            task["assignment_injection_id"] = None
            task["active_attempt_usage"] = None
            task["verified_branch_commit"] = None
            task["verified_at"] = None
            task["resume_decision"] = "dependency_reset"
            task["last_resume_reason"] = f"Dependency {missing_dep} is no longer completed"
            task["updated_at"] = now
            changed_task_ids.add(str(task_id))
            summary["downgraded_to_pending"] += 1
            changed = True
            if _task_is_integration(task):
                _reset_project_integration_result(project)

    integration_completed = any(_task_is_integration(task) and task.get("status") == "completed" for task in tasks.values())
    if not integration_completed:
        _reset_project_integration_result(project)

    _refresh_project_status(project)
    return sorted(changed_task_ids), summary


def _project_resume_preview(project, worker_swarm_ids=None, retry_failed=False, reverify_completed=True):
    if not isinstance(project, dict):
        raise RuntimeError("project not found")

    preview_project = copy.deepcopy(project)
    original_counts = _project_task_counts(preview_project)
    blocked_reason = None
    blocking_assignments = _project_live_assignment_details(preview_project)

    if str(preview_project.get("status") or "").strip().lower() == "completed":
        blocked_reason = "project is already completed"

    live_assignments = len(blocking_assignments) > 0
    if live_assignments:
        blocked_reason = (
            blocked_reason
            or "project has tasks still assigned to active swarms; terminate or wait for them before resuming"
        )

    normalized_workers = _normalize_resume_worker_swarm_ids(worker_swarm_ids)
    if normalized_workers is not None:
        preview_project["worker_swarm_ids"] = normalized_workers
    if not isinstance(preview_project.get("worker_swarm_ids"), list) or not preview_project.get("worker_swarm_ids"):
        blocked_reason = blocked_reason or "project has no worker swarms configured"

    changed_task_ids, summary = _reconcile_project_for_resume(
        preview_project,
        retry_failed=bool(retry_failed),
        reverify_completed=bool(reverify_completed),
    )
    resulting_counts = preview_project.get("task_counts") or _project_task_counts(preview_project)

    task_changes = []
    original_tasks = project.get("tasks") or {}
    preview_tasks = preview_project.get("tasks") or {}
    for task_id in preview_project.get("task_order") or list(preview_tasks.keys()):
        before = original_tasks.get(task_id) or {}
        after = preview_tasks.get(task_id) or {}
        before_status = str(before.get("status") or "pending")
        after_status = str(after.get("status") or "pending")
        resume_decision = after.get("resume_decision")
        changed = (
            task_id in changed_task_ids
            or before_status != after_status
            or bool(resume_decision)
        )
        if not changed:
            continue
        task_changes.append({
            "task_id": str(task_id),
            "title": str(after.get("title") or before.get("title") or task_id),
            "before_status": before_status,
            "after_status": after_status,
            "resume_decision": resume_decision,
            "reason": after.get("last_resume_reason"),
            "branch": after.get("branch") or before.get("branch"),
            "assigned_swarm_id": before.get("assigned_swarm_id"),
            "assigned_node_id": before.get("assigned_node_id"),
        })

    return {
        "project_id": preview_project.get("project_id"),
        "title": preview_project.get("title"),
        "status": preview_project.get("status"),
        "blocked": bool(blocked_reason),
        "blocked_reason": blocked_reason,
        "has_live_assignments": bool(live_assignments),
        "blocking_assignments": blocking_assignments,
        "blocking_swarms": [
            {
                "swarm_id": str(item.get("swarm_id") or ""),
                "swarm_alias": str(item.get("swarm_alias") or item.get("swarm_id") or ""),
                "swarm_status": str(item.get("swarm_status") or "running"),
            }
            for item in {
                str(entry.get("swarm_id") or ""): entry
                for entry in blocking_assignments
                if str(entry.get("swarm_id") or "").strip()
            }.values()
        ],
        "retry_failed": bool(retry_failed),
        "reverify_completed": bool(reverify_completed),
        "worker_swarm_ids": list(preview_project.get("worker_swarm_ids") or []),
        "counts_before": original_counts,
        "counts_after": resulting_counts,
        "summary": summary,
        "changed_task_ids": changed_task_ids,
        "task_changes": task_changes,
    }


def _resume_project_async(
    project_id,
    request_id,
    worker_swarm_ids=None,
    retry_failed=False,
    reverify_completed=True,
):
    def _run_project_resume():
        try:
            with SCHEDULER_LOCK:
                project = copy.deepcopy(PROJECTS.get(str(project_id)))
            if not project:
                raise RuntimeError("unknown project_id")
            if str(project.get("status") or "").strip().lower() == "completed":
                raise RuntimeError("project is already completed")
            if _project_has_live_assignments(project):
                raise RuntimeError("project has tasks still assigned to active swarms; terminate or wait for them before resuming")

            normalized_workers = _normalize_resume_worker_swarm_ids(worker_swarm_ids)
            if normalized_workers is not None:
                project["worker_swarm_ids"] = normalized_workers
            if not isinstance(project.get("worker_swarm_ids"), list) or not project.get("worker_swarm_ids"):
                raise RuntimeError("project has no worker swarms configured")

            changed_task_ids, summary = _reconcile_project_for_resume(
                project,
                retry_failed=bool(retry_failed),
                reverify_completed=bool(reverify_completed),
            )
            project["resume_count"] = int(project.get("resume_count") or 0) + 1
            project["last_resume_at"] = time.time()
            project["last_resumed_by_worker_swarm_ids"] = list(project.get("worker_swarm_ids") or [])
            project["resume_summary"] = summary
            project["last_error"] = None

            total_tasks = len(project.get("tasks") or {})
            completed_tasks = int((project.get("task_counts") or {}).get("completed", 0))
            if total_tasks > 0 and completed_tasks == total_tasks:
                project["status"] = "completed"
                with SCHEDULER_LOCK:
                    PROJECTS[str(project_id)] = project
                for task_id in changed_task_ids:
                    task = (project.get("tasks") or {}).get(str(task_id))
                    if task:
                        _sync_task_status_to_beads(project, task)
                _sync_project_to_beads(project)
                emit_event("project_resumed", {
                    "request_id": request_id,
                    "project_id": project_id,
                    "status": "completed",
                    "resume_summary": summary,
                })
                _emit_projects_updated()
                save_state()
                return

            preparation = {}
            for swarm_id in project.get("worker_swarm_ids") or []:
                swarm = SWARMS.get(str(swarm_id))
                provider = _provider_for_swarm(str(swarm_id))
                if not swarm or not provider:
                    raise RuntimeError(f"worker swarm unavailable: {swarm_id}")
                prepared = provider.prepare_repository(
                    str(swarm.get("job_id")),
                    str(project.get("repo_path")),
                    branch=project.get("base_branch"),
                    subdir=project.get("workspace_subdir") or "repo",
                )
                preparation[str(swarm_id)] = prepared
            project["repo_preparation"] = preparation
            project["status"] = "running"
            _refresh_project_status(project)

            with SCHEDULER_LOCK:
                PROJECTS[str(project_id)] = project
            for task_id in changed_task_ids:
                task = (project.get("tasks") or {}).get(str(task_id))
                if task:
                    _sync_task_status_to_beads(project, task)
            _sync_project_to_beads(project)
            emit_event("project_resumed", {
                "request_id": request_id,
                "project_id": project_id,
                "status": project.get("status"),
                "resume_summary": summary,
            })
            _emit_projects_updated()
            save_state()
            _dispatch_project_tasks(config=None)
        except Exception as e:
            with SCHEDULER_LOCK:
                current = PROJECTS.get(str(project_id))
                if current:
                    current["status"] = "error"
                    current["last_error"] = str(e)
                    current["updated_at"] = time.time()
            emit_event("command_rejected", {
                "request_id": request_id,
                "reason": str(e),
            })
            _emit_projects_updated()
            save_state()

    threading.Thread(target=_run_project_resume, daemon=True).start()


def _start_project_async(project_id, request_id):
    project = PROJECTS.get(str(project_id))
    if not project:
        emit_event("command_rejected", {
            "request_id": request_id,
            "reason": "unknown project_id",
        })
        return

    def _run_project_start():
        try:
            current = PROJECTS.get(str(project_id))
            if current:
                _sync_project_to_beads(current)
            preparation = {}
            for swarm_id in project.get("worker_swarm_ids") or []:
                swarm = SWARMS.get(str(swarm_id))
                provider = _provider_for_swarm(str(swarm_id))
                if not swarm or not provider:
                    raise RuntimeError(f"worker swarm unavailable: {swarm_id}")
                prepared = provider.prepare_repository(
                    str(swarm.get("job_id")),
                    str(project.get("repo_path")),
                    branch=project.get("base_branch"),
                    subdir=project.get("workspace_subdir") or "repo",
                )
                preparation[str(swarm_id)] = prepared
            with SCHEDULER_LOCK:
                current = PROJECTS.get(str(project_id))
                if not current:
                    return
                current["repo_preparation"] = preparation
                current["status"] = "running"
                _refresh_project_status(current)
            emit_event("project_started", {
                "request_id": request_id,
                "project_id": project_id,
                "status": "running",
            })
            _emit_projects_updated()
            save_state()
            _dispatch_project_tasks(config=None)
        except Exception as e:
            with SCHEDULER_LOCK:
                current = PROJECTS.get(str(project_id))
                if current:
                    current["status"] = "error"
                    current["last_error"] = str(e)
                    current["updated_at"] = time.time()
            emit_event("command_rejected", {
                "request_id": request_id,
                "reason": str(e),
            })
            _emit_projects_updated()
            save_state()

    threading.Thread(target=_run_project_start, daemon=True).start()


def _dispatch_project_tasks(config):
    scheduled = False
    with SCHEDULER_LOCK:
        project_ids = list(PROJECTS.keys())
    for project_id in project_ids:
        while True:
            with SCHEDULER_LOCK:
                project = PROJECTS.get(str(project_id))
                if not project or project.get("status") != "running":
                    break
                ready_task = None
                for task_id in project.get("task_order") or list((project.get("tasks") or {}).keys()):
                    task = (project.get("tasks") or {}).get(task_id)
                    if task and _project_task_is_ready(project, task_id):
                        ready_task = task
                        break
                if not ready_task:
                    _refresh_project_status(project)
                    break
            swarm_id, node_id = _project_first_idle_target(project)
            if swarm_id is None or node_id is None:
                break
            swarm = SWARMS.get(str(swarm_id))
            provider = _provider_for_swarm(str(swarm_id))
            if not swarm or not provider:
                break
            task_prompt = _build_project_task_prompt(project, ready_task)
            request_id = f"project:{project_id}:{ready_task.get('task_id')}:{uuid.uuid4().hex[:8]}"
            branch_name = _project_task_branch_name(project, ready_task)
            _mark_outstanding(str(swarm_id), node_id, +1)
            success, injection_id, error = perform_injection(
                config,
                provider,
                request_id,
                str(swarm_id),
                str(swarm.get("job_id")),
                int(node_id),
                task_prompt,
                count_outstanding=False,
            )
            if not success:
                _mark_outstanding(str(swarm_id), node_id, -1)
                with SCHEDULER_LOCK:
                    current_project = PROJECTS.get(str(project_id))
                    if current_project:
                        task = (current_project.get("tasks") or {}).get(str(ready_task.get("task_id")))
                        if task:
                            task["status"] = "failed"
                            task["last_error"] = error or "project injection failed"
                            task["updated_at"] = time.time()
                            _refresh_project_status(current_project)
                _emit_projects_updated()
                save_state()
                break
            project_ref = None
            task_ref = None
            with SCHEDULER_LOCK:
                current_project = PROJECTS.get(str(project_id))
                if current_project:
                    task = (current_project.get("tasks") or {}).get(str(ready_task.get("task_id")))
                    if task:
                        task["status"] = "assigned"
                        task["attempts"] = int(task.get("attempts") or 0) + 1
                        task["assigned_swarm_id"] = str(swarm_id)
                        task["assigned_node_id"] = int(node_id)
                        task["last_assigned_swarm_id"] = str(swarm_id)
                        task["last_assigned_node_id"] = int(node_id)
                        task["assignment_injection_id"] = injection_id
                        task["active_attempt_usage"] = None
                        task["branch"] = branch_name
                        task["last_error"] = None
                        task["updated_at"] = time.time()
                        _refresh_project_status(current_project)
                        project_ref = current_project
                        task_ref = task
            _sync_task_status_to_beads(project_ref, task_ref)
            scheduled = True
            _emit_projects_updated()
            save_state()
    return scheduled


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


def _first_quiescent_node_id(swarm_id):
    swarm = SWARMS.get(str(swarm_id))
    if not swarm:
        return None
    node_count = int(swarm.get("node_count") or 0)
    for node_id in range(node_count):
        if _is_node_quiescent(swarm_id, node_id):
            return node_id
    return None


def _first_available_node_id(swarm_id):
    swarm = SWARMS.get(str(swarm_id))
    if not swarm:
        return None
    node_count = int(swarm.get("node_count") or 0)
    if node_count <= 0:
        return None
    best_node = None
    best_score = None
    for node_id in range(node_count):
        key = _node_key(swarm_id, node_id)
        outstanding = int(NODE_OUTSTANDING.get(key, 0))
        active = 1 if bool(NODE_THREAD_ACTIVE.get(key, False)) else 0
        score = (outstanding, active, node_id)
        if best_score is None or score < best_score:
            best_score = score
            best_node = node_id
    return best_node


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

def _has_nonempty_text(value):
    return isinstance(value, str) and bool(value.strip())


def perform_injection(config, provider, request_id, swarm_id, job_id, node_id, content, count_outstanding=True):
    injection_id = str(uuid.uuid4())

    emit_event("inject_ack", {
        "request_id": request_id,
        "swarm_id": swarm_id,
        "injection_id": injection_id,
        "node_id": node_id,
        "prompt": content,
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

    if event_type == "worker_event":
        event_name = str(event.get("event") or "").strip()
        if not event_name:
            return None
        payload = event.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        return (
            event_name,
            {
                "swarm_id": swarm_id,
                "job_id": job_id,
                "node_id": node_id,
                "injection_id": injection_id,
                **payload,
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
                            available_decisions=available_decisions,
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
                    prefer_native_dialect = _approval_prefers_native_dialect(
                        method,
                        has_native_approval,
                    )
                    route_available_decisions = (
                        merged_native_available
                        if prefer_native_dialect
                        else merged_item_available
                    ) or merged_available
                    rpc_decision = _normalize_decision_for_available(
                        raw_decision,
                        approved_flag,
                        route_available_decisions,
                        prefer_native_dialect,
                        proposed_execpolicy_amendment,
                    )
                    rpc_decision = _coerce_to_advertised_decision(
                        rpc_decision,
                        route_available_decisions,
                        approved_flag,
                        prefer_native_dialect,
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
TCP_CLIENTS_LOCK = threading.Lock()

def run_daemon(config, providers):
    global ACTIVE_PROVIDER
    ACTIVE_PROVIDER = None

    # TCP control server
    import socket, sys

    def tcp_server():
        host = str(os.environ.get("CODESWARM_ROUTER_HOST") or "127.0.0.1").strip() or "127.0.0.1"
        port = int(os.environ.get("CODESWARM_ROUTER_PORT") or 8765)

        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((host, port))
        server.listen()

        print(f"TCP CONTROL READY {host}:{port}", file=sys.stderr, flush=True)

        while True:
            conn, addr = server.accept()
            conn.setblocking(False)
            with TCP_CLIENTS_LOCK:
                TCP_CLIENTS.append(conn)
            threading.Thread(target=handle_client, args=(conn,), daemon=True).start()

    def handle_client(conn):
        print("CLIENT CONNECTED", file=sys.stderr, flush=True)
        buffer = b""
        try:
            while True:
                try:
                    chunk = conn.recv(4096)
                except BlockingIOError:
                    time.sleep(0.01)
                    continue
                except TimeoutError:
                    continue
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
            with TCP_CLIENTS_LOCK:
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
    _dispatch_project_tasks(config)

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
                    suppress_emit = False
                    if event_name == "exec_approval_required":
                        approval_id = _register_canonical_exec_approval(data)
                        if approval_id and not data.get("approval_id"):
                            data = {**data, "approval_id": approval_id, "approvals_version": APPROVALS_VERSION}
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
                            _dispatch_pending_project_plans(config)
                            _dispatch_project_tasks(config)
                    if event_name == "turn_complete":
                        _mark_outstanding(data.get("swarm_id"), data.get("node_id"), -1)
                        _dispatch_inter_swarm_queue(config)
                        _dispatch_pending_project_plans(config)
                        _dispatch_project_tasks(config)
                    if event_name == "task_complete":
                        # Some traces emit task_complete without a matching turn_complete;
                        # reconcile outstanding count to avoid idle-queue starvation.
                        _mark_outstanding(data.get("swarm_id"), data.get("node_id"), -1)
                        plan_id = _find_pending_project_plan_by_injection(data.get("injection_id"))
                        if plan_id:
                            _record_project_plan_result(plan_id, data)
                        project_id, task_id = _find_project_and_task_by_injection(data.get("injection_id"))
                        if project_id and task_id:
                            _record_project_task_result(project_id, task_id, data)
                        _dispatch_inter_swarm_queue(config)
                        _dispatch_pending_project_plans(config)
                        _dispatch_project_tasks(config)
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
                        plan_id = _find_pending_project_plan_by_injection(data.get("injection_id"))
                        if plan_id:
                            _record_project_plan_result(plan_id, {
                                "last_agent_message": data.get("content"),
                            })
                        project_id, task_id = _find_project_and_task_by_injection(data.get("injection_id"))
                        if project_id and task_id:
                            _record_project_task_result(project_id, task_id, {
                                "last_agent_message": data.get("content"),
                            })
                        if should_reconcile:
                            _mark_outstanding(data.get("swarm_id"), data.get("node_id"), -1)
                        _dispatch_inter_swarm_queue(config)
                        _dispatch_pending_project_plans(config)
                        _dispatch_project_tasks(config)
                    if event_name == "usage":
                        _update_project_usage_for_injection(data)
                    if event_name in ("command_started", "command_completed", "filechange_started", "filechange_completed"):
                        call_id = data.get("call_id")
                        node_id = data.get("node_id")
                        job_id = data.get("job_id")
                        if isinstance(call_id, str) and call_id and isinstance(node_id, int) and isinstance(job_id, str):
                            _prune_pending_approval_for_call(job_id, node_id, call_id, event_name)
                    if event_name == "exec_approval_resolved":
                        call_id = data.get("call_id")
                        node_id = data.get("node_id")
                        job_id = data.get("job_id")
                        if isinstance(call_id, str) and call_id and isinstance(node_id, int) and isinstance(job_id, str):
                            _prune_pending_approval_for_call(job_id, node_id, call_id, "worker_exec_approval_resolved")
                            suppress_emit = True
                    if not suppress_emit:
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
                if not isinstance(system_prompt, str):
                    system_prompt = ""
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
                if bool(provider_spec.get("disabled")):
                    emit_event("command_rejected", {
                        "request_id": request_id,
                        "reason": str(provider_spec.get("disabled_reason") or f"provider disabled: {provider_id}")
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

                def _launch_progress(
                    stage,
                    message,
                    launch_request_id=launch_request_id,
                    launch_provider_backend=launch_provider_backend,
                    launch_provider_id=launch_provider_id,
                ):
                    emit_event("swarm_launch_progress", {
                        "request_id": launch_request_id,
                        "provider": launch_provider_backend,
                        "provider_id": launch_provider_id,
                        "stage": str(stage),
                        "message": str(message),
                        "timestamp": time.time(),
                    })

                def _run_launch(
                    launch_provider_obj=launch_provider_obj,
                    launch_nodes=launch_nodes,
                    launch_agents_md_content=launch_agents_md_content,
                    launch_agents_bundle=launch_agents_bundle,
                    launch_effective_params=launch_effective_params,
                    launch_request_id=launch_request_id,
                    launch_system_prompt=launch_system_prompt,
                    launch_provider_ref=launch_provider_ref,
                    launch_provider_backend=launch_provider_backend,
                    launch_provider_id=launch_provider_id,
                    _launch_progress=_launch_progress,
                ):
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
                    agent_runtime = str(
                        launch_effective_params.get("agent_runtime")
                        or launch_effective_params.get("worker_mode")
                        or "codex"
                    ).strip().lower()
                    agent_model = _resolve_swarm_agent_model(
                        config,
                        agent_runtime,
                        launch_effective_params,
                        provider_backend=launch_provider_backend,
                    )
                    pricing_model = _resolve_swarm_pricing_model(
                        config,
                        agent_runtime,
                        launch_effective_params,
                        agent_model=agent_model,
                        provider_backend=launch_provider_backend,
                    )

                    SWARMS[swarm_id] = {
                        "job_id": job_id,
                        "node_count": launch_nodes,
                        "system_prompt": launch_system_prompt,
                        "status": "running",
                        "agent_runtime": agent_runtime,
                        "agent_model": agent_model,
                        "pricing_model": pricing_model,
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
                        "agent_runtime": SWARMS[swarm_id].get("agent_runtime"),
                        "agent_model": SWARMS[swarm_id].get("agent_model"),
                        "pricing_model": SWARMS[swarm_id].get("pricing_model"),
                        "claude_env_profile": (
                            SWARMS[swarm_id].get("provider_params", {}) or {}
                        ).get("claude_env_profile"),
                    })

                    if _has_nonempty_text(launch_system_prompt):
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

            elif command == "project_list":
                emit_event("project_list", {
                    "request_id": request_id,
                    "projects": _project_snapshot(),
                })
                _emit_projects_updated()

            elif command == "project_create":
                title = str(payload.get("title") or "").strip()
                worker_swarm_ids = payload.get("worker_swarm_ids")
                raw_tasks = payload.get("tasks")
                try:
                    repo_meta = _resolve_project_repo_spec(
                        repo_path=payload.get("repo_path") or "",
                        repo_mode=payload.get("repo_mode"),
                        github_owner=payload.get("github_owner"),
                        github_repo=payload.get("github_repo"),
                        github_create_if_missing=payload.get("github_create_if_missing"),
                        github_visibility=payload.get("github_visibility") or "private",
                    )
                except Exception as e:
                    emit_event("command_rejected", {
                        "request_id": request_id,
                        "reason": str(e),
                    })
                    continue
                repo_path = str(repo_meta.get("repo_path") or "").strip()
                base_branch = str(
                    payload.get("base_branch") or repo_meta.get("default_branch") or "main"
                ).strip() or "main"
                workspace_subdir = str(payload.get("workspace_subdir") or "repo").strip() or "repo"
                auto_start = bool(payload.get("auto_start"))
                try:
                    project = _create_project_record(
                        title,
                        repo_path,
                        worker_swarm_ids,
                        raw_tasks,
                        base_branch=base_branch,
                        workspace_subdir=workspace_subdir,
                        repo_meta=repo_meta,
                    )
                except Exception as e:
                    emit_event("command_rejected", {
                        "request_id": request_id,
                        "reason": str(e),
                    })
                    continue
                emit_event("project_created", {
                    "request_id": request_id,
                    "project_id": project.get("project_id"),
                    "title": project.get("title"),
                })
                if auto_start:
                    with SCHEDULER_LOCK:
                        current = PROJECTS.get(str(project.get("project_id")))
                        if current:
                            current["status"] = "starting"
                            current["updated_at"] = time.time()
                    _emit_projects_updated()
                    save_state()
                    _start_project_async(project.get("project_id"), request_id)

            elif command == "project_plan":
                title = str(payload.get("title") or "").strip()
                spec_text = str(payload.get("spec") or "").strip()
                planner_swarm_id = str(payload.get("planner_swarm_id") or "").strip()
                worker_swarm_ids = payload.get("worker_swarm_ids")
                try:
                    repo_meta = _resolve_project_repo_spec(
                        repo_path=payload.get("repo_path") or "",
                        repo_mode=payload.get("repo_mode"),
                        github_owner=payload.get("github_owner"),
                        github_repo=payload.get("github_repo"),
                        github_create_if_missing=payload.get("github_create_if_missing"),
                        github_visibility=payload.get("github_visibility") or "private",
                    )
                except Exception as e:
                    emit_event("command_rejected", {
                        "request_id": request_id,
                        "reason": str(e),
                    })
                    continue
                repo_path = str(repo_meta.get("repo_path") or "").strip()
                base_branch = str(
                    payload.get("base_branch") or repo_meta.get("default_branch") or "main"
                ).strip() or "main"
                workspace_subdir = str(payload.get("workspace_subdir") or "repo").strip() or "repo"
                auto_start = bool(payload.get("auto_start"))

                if not title or not repo_path or not spec_text or not planner_swarm_id:
                    emit_event("command_rejected", {
                        "request_id": request_id,
                        "reason": "project_plan requires title, repo_path, spec, and planner_swarm_id",
                    })
                    continue
                planner_swarm = SWARMS.get(planner_swarm_id)
                if not planner_swarm:
                    emit_event("command_rejected", {
                        "request_id": request_id,
                        "reason": "unknown planner_swarm_id",
                    })
                    continue
                planner_provider = _provider_for_swarm(planner_swarm_id)
                if not planner_provider:
                    emit_event("command_rejected", {
                        "request_id": request_id,
                        "reason": "provider unavailable for planner swarm",
                    })
                    continue
                prompt = _build_project_plan_prompt(
                    title,
                    repo_path,
                    spec_text,
                    base_branch,
                    workspace_subdir,
                    repo_meta.get("repo_label"),
                    repo_meta.get("repo_remote_url"),
                )
                plan_id = str(uuid.uuid4())
                with SCHEDULER_LOCK:
                    PENDING_PROJECT_PLANS[plan_id] = {
                        "plan_id": plan_id,
                        "request_id": request_id,
                        "title": title,
                        "repo_path": repo_path,
                        "repo_mode": repo_meta.get("repo_mode"),
                        "repo_label": repo_meta.get("repo_label"),
                        "repo_remote_url": repo_meta.get("repo_remote_url"),
                        "github_owner": repo_meta.get("github_owner"),
                        "github_repo": repo_meta.get("github_repo"),
                        "github_visibility": repo_meta.get("github_visibility"),
                        "github_create_if_missing": bool(repo_meta.get("github_create_if_missing")),
                        "spec": spec_text,
                        "planner_swarm_id": planner_swarm_id,
                        "planner_node_id": None,
                        "worker_swarm_ids": worker_swarm_ids if isinstance(worker_swarm_ids, list) else [],
                        "base_branch": base_branch,
                        "workspace_subdir": workspace_subdir,
                        "injection_id": None,
                        "prompt": prompt,
                        "auto_start": auto_start,
                        "status": "queued",
                        "created_at": time.time(),
                        "updated_at": time.time(),
                    }
                save_state()
                emit_event("project_plan_queued", {
                    "request_id": request_id,
                    "plan_id": plan_id,
                    "planner_swarm_id": planner_swarm_id,
                })
                _dispatch_pending_project_plans(config)

            elif command == "project_start":
                project_id = str(payload.get("project_id") or "").strip()
                project = PROJECTS.get(project_id)
                if not project:
                    emit_event("command_rejected", {
                        "request_id": request_id,
                        "reason": "unknown project_id",
                    })
                    continue
                if project.get("status") == "running":
                    emit_event("project_started", {
                        "request_id": request_id,
                        "project_id": project_id,
                        "status": "running",
                    })
                    _emit_projects_updated()
                    _dispatch_project_tasks(config)
                    continue

                with SCHEDULER_LOCK:
                    project["status"] = "starting"
                    project["updated_at"] = time.time()
                _emit_projects_updated()
                save_state()
                _start_project_async(project_id, request_id)

            elif command == "project_resume":
                project_id = str(payload.get("project_id") or "").strip()
                project = PROJECTS.get(project_id)
                if not project:
                    emit_event("command_rejected", {
                        "request_id": request_id,
                        "reason": "unknown project_id",
                    })
                    continue
                if str(project.get("status") or "").strip().lower() == "completed":
                    emit_event("command_rejected", {
                        "request_id": request_id,
                        "reason": "project is already completed",
                    })
                    continue

                with SCHEDULER_LOCK:
                    project["status"] = "resuming"
                    project["updated_at"] = time.time()
                _emit_projects_updated()
                save_state()
                _resume_project_async(
                    project_id,
                    request_id,
                    worker_swarm_ids=payload.get("worker_swarm_ids"),
                    retry_failed=bool(payload.get("retry_failed")),
                    reverify_completed=bool(payload.get("reverify_completed", True)),
                )

            elif command == "project_resume_preview":
                project_id = str(payload.get("project_id") or "").strip()
                project = PROJECTS.get(project_id)
                if not project:
                    emit_event("command_rejected", {
                        "request_id": request_id,
                        "reason": "unknown project_id",
                    })
                    continue
                try:
                    preview = _project_resume_preview(
                        project,
                        worker_swarm_ids=payload.get("worker_swarm_ids"),
                        retry_failed=bool(payload.get("retry_failed")),
                        reverify_completed=bool(payload.get("reverify_completed", True)),
                    )
                except Exception as e:
                    emit_event("command_rejected", {
                        "request_id": request_id,
                        "reason": str(e),
                    })
                    continue
                emit_event("project_resume_preview", {
                    "request_id": request_id,
                    "project_id": project_id,
                    "preview": preview,
                })

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
                    prefer_native_dialect = _approval_prefers_native_dialect(
                        approval_method,
                        has_native_approval,
                    )
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
    global PROVIDER_SPECS, PROVIDERS, MODEL_PRICING
    requested_provider_specs = get_provider_specs(config)
    PROVIDERS, PROVIDER_SPECS = build_providers(config, requested_provider_specs)
    MODEL_PRICING = _load_model_pricing_catalog(config)
    disabled_specs = [spec for spec in PROVIDER_SPECS if bool(spec.get("disabled"))]
    if disabled_specs:
        for spec in disabled_specs:
            startup_log(
                f"provider {spec.get('id') or spec.get('provider_ref') or 'unknown'} disabled: {spec.get('disabled_reason') or 'initialization failed'}"
            )

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

    reconcile(PROVIDERS, config)

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
