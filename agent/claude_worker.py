#!/usr/bin/env python3
import asyncio
import json
import os
import signal
import time
import uuid
from pathlib import Path


def write_event(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload) + "\n")


def emit_worker_event(outbox_path: Path, job_id: str, node_id: int, injection_id: str | None, event_name: str, payload: dict | None = None) -> None:
    write_event(
        outbox_path,
        {
            "type": "worker_event",
            "job_id": job_id,
            "node_id": node_id,
            "injection_id": injection_id,
            "event": event_name,
            "payload": payload or {},
        },
    )


def emit_worker_error(outbox_path: Path, job_id: str, node_id: int, injection_id: str | None, message: str) -> None:
    write_event(
        outbox_path,
        {
            "type": "worker_error",
            "job_id": job_id,
            "node_id": node_id,
            "injection_id": injection_id,
            "error": message,
        },
    )


def emit_complete(outbox_path: Path, job_id: str, node_id: int, injection_id: str | None) -> None:
    write_event(
        outbox_path,
        {
            "type": "complete",
            "job_id": job_id,
            "node_id": node_id,
            "injection_id": injection_id,
        },
    )


def _extract_text_from_content_blocks(blocks) -> str:
    if not isinstance(blocks, list):
        return ""
    parts: list[str] = []
    for block in blocks:
        text = getattr(block, "text", None)
        if isinstance(text, str) and text:
            parts.append(text)
    return "".join(parts)


def _extract_reasoning_from_content_blocks(blocks) -> str:
    if not isinstance(blocks, list):
        return ""
    parts: list[str] = []
    for block in blocks:
        thinking = getattr(block, "thinking", None)
        if isinstance(thinking, str) and thinking:
            parts.append(thinking)
    return "".join(parts)


def _normalize_usage_dict(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None

    def pick(*keys):
        for key in keys:
            value = raw.get(key)
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                return int(value)
            if isinstance(value, str):
                try:
                    return int(value)
                except Exception:
                    continue
        return None

    input_tokens = pick("input_tokens", "inputTokens")
    cached_input_tokens = pick("cached_input_tokens", "cachedInputTokens", "cache_creation_input_tokens", "cacheCreationInputTokens")
    output_tokens = pick("output_tokens", "outputTokens")
    reasoning_output_tokens = pick("reasoning_output_tokens", "reasoningOutputTokens", "thinking_tokens", "thinkingTokens")
    total_tokens = pick("total_tokens", "totalTokens")

    if total_tokens is None:
        total_tokens = sum(
            value or 0
            for value in (
                input_tokens,
                cached_input_tokens,
                output_tokens,
                reasoning_output_tokens,
            )
        )

    return {
        "total_tokens": int(total_tokens or 0),
        "input_tokens": int(input_tokens or 0),
        "cached_input_tokens": int(cached_input_tokens or 0),
        "output_tokens": int(output_tokens or 0),
        "reasoning_output_tokens": int(reasoning_output_tokens or 0),
    }


def _usage_delta(current: dict, previous: dict | None) -> dict:
    if not isinstance(previous, dict):
        return dict(current)
    delta = {}
    for key, current_value in current.items():
        previous_value = int(previous.get(key) or 0)
        value = int(current_value or 0) - previous_value
        if value < 0:
            value = int(current_value or 0)
        delta[key] = value
    return delta


def _usage_has_nonzero_values(usage: dict | None) -> bool:
    if not isinstance(usage, dict):
        return False
    return any(int(value or 0) > 0 for value in usage.values())


def _tool_event_from_block(block) -> tuple[str, dict] | None:
    name = str(getattr(block, "name", "") or "").strip()
    call_id = str(getattr(block, "id", "") or "").strip()
    input_payload = getattr(block, "input", None)
    if not name or not call_id or not isinstance(input_payload, dict):
        return None
    if name == "Bash":
        return (
            "command_started",
            {
                "call_id": call_id,
                "command": input_payload.get("command"),
                "cwd": input_payload.get("cwd"),
                "raw": {"tool_name": name, "tool_input": input_payload},
            },
        )
    if name in {"Edit", "Write", "MultiEdit"}:
        return (
            "filechange_started",
            {
                "call_id": call_id,
                "raw": {"tool_name": name, "tool_input": input_payload},
            },
        )
    return None


def _tool_start_event_from_name_input(tool_name: str, call_id: str, tool_input: dict | None) -> tuple[str, dict] | None:
    payload = tool_input if isinstance(tool_input, dict) else {}
    name = str(tool_name or "").strip()
    if not name or not call_id:
        return None
    if name == "Bash":
        return (
            "command_started",
            {
                "call_id": call_id,
                "command": payload.get("command"),
                "cwd": payload.get("cwd"),
                "raw": {"tool_name": name, "tool_input": payload},
            },
        )
    if name in {"Edit", "Write", "MultiEdit"}:
        return (
            "filechange_started",
            {
                "call_id": call_id,
                "raw": {"tool_name": name, "tool_input": payload},
            },
        )
    return None


def _tool_completion_event(tool_state: dict, block) -> tuple[str, dict] | None:
    if not isinstance(tool_state, dict):
        return None
    tool_kind = tool_state.get("kind")
    call_id = str(getattr(block, "tool_use_id", "") or "").strip()
    if not call_id:
        return None
    raw_content = getattr(block, "content", None)
    status = "error" if bool(getattr(block, "is_error", False)) else "ok"
    payload = {
        "call_id": call_id,
        "status": status,
        "raw": {
            "content": raw_content,
            "is_error": getattr(block, "is_error", None),
            "tool_name": tool_state.get("tool_name"),
        },
    }
    if tool_kind == "command":
        payload["command"] = tool_state.get("command")
        payload["cwd"] = tool_state.get("cwd")
        if isinstance(raw_content, str):
            payload["stdout"] = raw_content
        return ("command_completed", payload)
    if tool_kind == "filechange":
        return ("filechange_completed", payload)
    return None


def _decision_is_approved(decision, approved_hint=None) -> bool:
    if isinstance(approved_hint, bool):
        return approved_hint
    if isinstance(decision, str):
        return decision in ("accept", "acceptForSession", "approved", "approved_for_session")
    if isinstance(decision, dict):
        return any(
            key in decision
            for key in ("approved_execpolicy_amendment", "acceptWithExecpolicyAmendment")
        )
    return False


def _tool_permission_summary(tool_name: str, tool_input: dict | None, workspace_dir: str) -> tuple[object, str, str | None]:
    payload = tool_input if isinstance(tool_input, dict) else {}
    name = str(tool_name or "").strip()
    if name == "Bash":
        return (
            payload.get("command"),
            "Approve Claude Bash command",
            str(payload.get("cwd") or workspace_dir),
        )
    if name in {"Edit", "Write", "MultiEdit"}:
        command = {"tool_name": name, "input": payload}
        return (command, "Approve Claude file changes", str(payload.get("cwd") or workspace_dir))
    return (
        {"tool_name": name or "unknown", "input": payload},
        f"Approve Claude tool use: {name or 'unknown'}",
        str(payload.get("cwd") or workspace_dir),
    )


class ClaudeWorker:
    def __init__(self):
        self.job_id = os.environ["CODESWARM_JOB_ID"]
        self.node_id = int(os.environ["CODESWARM_NODE_ID"])
        self.base = Path(os.environ["CODESWARM_BASE_DIR"])
        self.inbox_path = self.base / "mailbox" / "inbox" / f"{self.job_id}_{self.node_id:02d}.jsonl"
        self.outbox_path = self.base / "mailbox" / "outbox" / f"{self.job_id}_{self.node_id:02d}.jsonl"
        self.workspace_dir = os.getcwd()
        self.workspace_root = Path(self.workspace_dir).resolve()
        self.heartbeat_path = self.workspace_root / "heartbeat.json"
        self.stderr_log_path = self.workspace_root / "claude.stderr.log"
        self.fresh_thread_per_injection = os.environ.get("CODESWARM_FRESH_THREAD_PER_INJECTION", "").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        self.approval_policy = str(os.environ.get("CODESWARM_ASK_FOR_APPROVAL") or "").strip().lower()
        self.permission_mode = str(os.environ.get("CODESWARM_CLAUDE_PERMISSION_MODE") or "").strip() or "bypassPermissions"
        self.claude_model = str(os.environ.get("CODESWARM_CLAUDE_MODEL") or "").strip()
        self.claude_cli_path = str(os.environ.get("CODESWARM_CLAUDE_CLI_PATH") or "").strip()
        self.shutdown_requested = False
        self.inbox_offset_bytes = 0
        self.last_heartbeat_at = 0.0
        self.heartbeat_interval_seconds = 1.0
        self.persistent_preamble: str | None = None
        self.client = None
        self.pending_injections: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
        self.pending_approval_futures: dict[str, asyncio.Future] = {}
        self.pending_tool_states: dict[str, dict] = {}
        self.current_injection_id: str | None = None

    async def _load_sdk(self):
        try:
            from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, ResultMessage, SystemMessage, UserMessage  # type: ignore
            from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny  # type: ignore
        except Exception as e:
            emit_worker_error(
                self.outbox_path,
                self.job_id,
                self.node_id,
                None,
                f"claude_sdk_unavailable: {e}",
            )
            return None
        return {
            "AssistantMessage": AssistantMessage,
            "ClaudeAgentOptions": ClaudeAgentOptions,
            "ClaudeSDKClient": ClaudeSDKClient,
            "ResultMessage": ResultMessage,
            "SystemMessage": SystemMessage,
            "UserMessage": UserMessage,
            "PermissionResultAllow": PermissionResultAllow,
            "PermissionResultDeny": PermissionResultDeny,
        }

    def _build_options(self, sdk_symbols):
        ClaudeAgentOptions = sdk_symbols["ClaudeAgentOptions"]
        kwargs = {
            "cwd": self.workspace_dir,
            "permission_mode": self.permission_mode,
            "include_partial_messages": True,
        }
        if self.claude_model:
            kwargs["model"] = self.claude_model
        if self.claude_cli_path:
            kwargs["cli_path"] = self.claude_cli_path
        if self.approval_policy != "never":
            kwargs["can_use_tool"] = self._handle_tool_permission
        return ClaudeAgentOptions(**kwargs)

    async def _ensure_client(self, sdk_symbols):
        if self.client is not None:
            return self.client
        ClaudeSDKClient = sdk_symbols["ClaudeSDKClient"]
        self.client = ClaudeSDKClient(options=self._build_options(sdk_symbols))
        await self.client.connect()
        return self.client

    async def _handle_tool_permission(self, tool_name: str, tool_input: dict, context):
        PermissionResultAllow = (await self._load_sdk())["PermissionResultAllow"]
        PermissionResultDeny = (await self._load_sdk())["PermissionResultDeny"]

        if self.approval_policy == "never":
            return PermissionResultAllow()

        call_id = str(getattr(context, "tool_use_id", "") or "").strip() or str(uuid.uuid4())
        command, reason, cwd = _tool_permission_summary(tool_name, tool_input, self.workspace_dir)
        future = self.pending_approval_futures.get(call_id)
        if future is None or future.done():
            future = asyncio.get_running_loop().create_future()
            self.pending_approval_futures[call_id] = future

        emit_worker_event(
            self.outbox_path,
            self.job_id,
            self.node_id,
            self.current_injection_id,
            "exec_approval_required",
            {
                "call_id": call_id,
                "command": command,
                "reason": reason,
                "cwd": cwd,
                "available_decisions": ["accept", "cancel"],
                "approval_method": "claude/can_use_tool",
                "raw": {
                    "tool_name": tool_name,
                    "tool_input": tool_input,
                    "agent_id": getattr(context, "agent_id", None),
                    "suggestions": [
                        suggestion.to_dict() if hasattr(suggestion, "to_dict") else str(suggestion)
                        for suggestion in (getattr(context, "suggestions", None) or [])
                    ],
                },
            },
        )

        while not self.shutdown_requested and not future.done():
            await self._poll_inbox_once()
            await asyncio.sleep(0.1)

        self.pending_approval_futures.pop(call_id, None)
        if self.shutdown_requested:
            return PermissionResultDeny(message="Worker shutting down", interrupt=True)

        decision_payload = future.result()
        approved = _decision_is_approved(
            decision_payload.get("decision"),
            decision_payload.get("approved"),
        )
        if not approved:
            self.pending_tool_states.pop(call_id, None)
            emit_worker_event(
                self.outbox_path,
                self.job_id,
                self.node_id,
                self.current_injection_id,
                "exec_approval_resolved",
                {
                    "call_id": call_id,
                    "approved": False,
                    "decision": decision_payload.get("decision"),
                },
            )
            return PermissionResultDeny(message="Denied by Codeswarm approval", interrupt=False)

        pending_tool = self.pending_tool_states.get(call_id) or {}
        event_name = pending_tool.get("event_name")
        payload = pending_tool.get("payload")
        if not isinstance(event_name, str) or not isinstance(payload, dict):
            derived = _tool_start_event_from_name_input(tool_name, call_id, tool_input)
            if derived:
                event_name, payload = derived
        if isinstance(event_name, str) and isinstance(payload, dict):
            emit_worker_event(
                self.outbox_path,
                self.job_id,
                self.node_id,
                self.current_injection_id,
                event_name,
                payload,
            )

        return PermissionResultAllow()

    def _write_heartbeat(self, force: bool = False):
        now = time.time()
        if not force and (now - self.last_heartbeat_at) < self.heartbeat_interval_seconds:
            return
        self.heartbeat_path.write_text(
            json.dumps(
                {
                    "timestamp": now,
                    "job_id": self.job_id,
                    "node_id": self.node_id,
                    "worker_type": "claude",
                    "pid": os.getpid(),
                }
            ),
            encoding="utf-8",
        )
        self.last_heartbeat_at = now

    async def _heartbeat_loop(self):
        while not self.shutdown_requested:
            self._write_heartbeat()
            await asyncio.sleep(0.5)

    async def _process_message(self, sdk_symbols, injection_id: str, message, state: dict):
        AssistantMessage = sdk_symbols["AssistantMessage"]
        ResultMessage = sdk_symbols["ResultMessage"]
        UserMessage = sdk_symbols["UserMessage"]
        SystemMessage = sdk_symbols["SystemMessage"]

        if isinstance(message, AssistantMessage):
            content_blocks = getattr(message, "content", None)
            text = _extract_text_from_content_blocks(content_blocks)
            if text:
                previous_text = state.get("assistant_text", "")
                if text.startswith(previous_text):
                    delta = text[len(previous_text):]
                else:
                    delta = text
                if delta:
                    emit_worker_event(
                        self.outbox_path,
                        self.job_id,
                        self.node_id,
                        injection_id,
                        "assistant_delta",
                        {"content": delta},
                    )
                state["assistant_text"] = text

            reasoning = _extract_reasoning_from_content_blocks(content_blocks)
            if reasoning:
                previous_reasoning = state.get("reasoning_text", "")
                if reasoning.startswith(previous_reasoning):
                    delta = reasoning[len(previous_reasoning):]
                else:
                    delta = reasoning
                if delta:
                    emit_worker_event(
                        self.outbox_path,
                        self.job_id,
                        self.node_id,
                        injection_id,
                        "reasoning_delta",
                        {"content": delta},
                    )
                state["reasoning_text"] = reasoning

            if isinstance(content_blocks, list):
                for block in content_blocks:
                    started = _tool_event_from_block(block)
                    if started:
                        event_name, payload = started
                        call_id = str(payload.get("call_id") or "")
                        if call_id:
                            if event_name == "command_started":
                                state["active_tools"][call_id] = {
                                    "kind": "command",
                                    "tool_name": "Bash",
                                    "command": payload.get("command"),
                                    "cwd": payload.get("cwd"),
                                }
                            else:
                                state["active_tools"][call_id] = {
                                    "kind": "filechange",
                                    "tool_name": (payload.get("raw") or {}).get("tool_name"),
                                }
                            self.pending_tool_states[call_id] = {
                                "event_name": event_name,
                                "payload": payload,
                            }
                        if self.approval_policy == "never":
                            emit_worker_event(
                                self.outbox_path,
                                self.job_id,
                                self.node_id,
                                injection_id,
                                event_name,
                                payload,
                            )

            usage_payload = getattr(message, "usage", None)
            await self._emit_usage_if_present(injection_id, usage_payload, state, source="claude/assistant_message")
            return

        if isinstance(message, UserMessage):
            content_blocks = getattr(message, "content", None)
            if isinstance(content_blocks, list):
                for block in content_blocks:
                    tool_use_id = str(getattr(block, "tool_use_id", "") or "").strip()
                    tool_state = state["active_tools"].pop(tool_use_id, None)
                    completed = _tool_completion_event(tool_state, block)
                    if completed:
                        event_name, payload = completed
                        self.pending_tool_states.pop(tool_use_id, None)
                        emit_worker_event(
                            self.outbox_path,
                            self.job_id,
                            self.node_id,
                            injection_id,
                            event_name,
                            payload,
                        )
            return

        if isinstance(message, SystemMessage):
            subtype = str(getattr(message, "subtype", "") or "").strip()
            if subtype == "task_started":
                emit_worker_event(
                    self.outbox_path,
                    self.job_id,
                    self.node_id,
                    injection_id,
                    "task_started",
                    {"raw": getattr(message, "data", None)},
                )
            elif subtype == "task_notification":
                emit_worker_event(
                    self.outbox_path,
                    self.job_id,
                    self.node_id,
                    injection_id,
                    "task_complete",
                    {
                        "last_agent_message": state.get("assistant_text") or getattr(message, "summary", None),
                        "raw": getattr(message, "data", None),
                    },
                )
                usage_payload = getattr(message, "usage", None)
                await self._emit_usage_if_present(injection_id, usage_payload, state, source="claude/task_notification")
            return

        if isinstance(message, ResultMessage):
            usage_payload = getattr(message, "model_usage", None) or getattr(message, "usage", None)
            await self._emit_usage_if_present(injection_id, usage_payload, state, source="claude/result_message")
            state["result_message"] = {
                "result": getattr(message, "result", None),
                "errors": getattr(message, "errors", None),
                "is_error": bool(getattr(message, "is_error", False)),
            }
            return

    async def _emit_usage_if_present(self, injection_id: str, raw_usage, state: dict, source: str):
        current = _normalize_usage_dict(raw_usage)
        if not current:
            return
        previous = state.get("usage")
        delta = _usage_delta(current, previous)
        if not _usage_has_nonzero_values(current):
            return
        if previous is not None and not _usage_has_nonzero_values(delta):
            return
        state["usage"] = current
        emit_worker_event(
            self.outbox_path,
            self.job_id,
            self.node_id,
            injection_id,
            "usage",
            {
                **current,
                "last_total_tokens": int(delta.get("total_tokens") or 0),
                "last_input_tokens": int(delta.get("input_tokens") or 0),
                "last_cached_input_tokens": int(delta.get("cached_input_tokens") or 0),
                "last_output_tokens": int(delta.get("output_tokens") or 0),
                "last_reasoning_output_tokens": int(delta.get("reasoning_output_tokens") or 0),
                "usage_source": source,
            },
        )

    async def _run_query(self, sdk_symbols, injection_id: str, prompt: str, use_persistent_client: bool):
        state = {
            "assistant_text": "",
            "reasoning_text": "",
            "usage": None,
            "active_tools": {},
            "result_message": None,
        }
        emit_worker_event(
            self.outbox_path,
            self.job_id,
            self.node_id,
            injection_id,
            "turn_started",
            {},
        )
        self.current_injection_id = injection_id
        try:
            if use_persistent_client:
                client = await self._ensure_client(sdk_symbols)
                await client.query(prompt)
                async for message in client.receive_response():
                    await self._process_message(sdk_symbols, injection_id, message, state)
            else:
                ClaudeSDKClient = sdk_symbols["ClaudeSDKClient"]
                async with ClaudeSDKClient(options=self._build_options(sdk_symbols)) as client:
                    await client.query(prompt)
                    async for message in client.receive_response():
                        await self._process_message(sdk_symbols, injection_id, message, state)
        except Exception as e:
            emit_worker_error(
                self.outbox_path,
                self.job_id,
                self.node_id,
                injection_id,
                f"claude_query_failed: {e}",
            )
        finally:
            self.current_injection_id = None
            self.pending_tool_states.clear()
            final_text = state.get("assistant_text") or ""
            result_message = state.get("result_message") or {}
            if not final_text and isinstance(result_message.get("result"), str):
                final_text = str(result_message.get("result") or "")
            if final_text:
                emit_worker_event(
                    self.outbox_path,
                    self.job_id,
                    self.node_id,
                    injection_id,
                    "assistant",
                    {
                        "content": final_text,
                        "final_answer": True,
                    },
                )
            emit_worker_event(
                self.outbox_path,
                self.job_id,
                self.node_id,
                injection_id,
                "turn_complete",
                {},
            )
            emit_complete(self.outbox_path, self.job_id, self.node_id, injection_id)

    async def _handle_user_injection(self, sdk_symbols, injection_id: str, content: str):
        if self.fresh_thread_per_injection:
            if self.persistent_preamble is None:
                self.persistent_preamble = content
                emit_complete(self.outbox_path, self.job_id, self.node_id, injection_id)
                return
            prompt = content
            if isinstance(self.persistent_preamble, str) and self.persistent_preamble.strip():
                prompt = f"{self.persistent_preamble.rstrip()}\n\n{content}"
            await self._run_query(sdk_symbols, injection_id, prompt, use_persistent_client=False)
            return
        await self._run_query(sdk_symbols, injection_id, content, use_persistent_client=True)

    def _record_control_decision(self, payload: dict) -> None:
        if not isinstance(payload, dict):
            return
        if payload.get("type") == "rpc_response":
            result = payload.get("result")
            call_id = payload.get("call_id")
            if not isinstance(call_id, str) or not call_id.strip():
                return
            decision = result.get("decision") if isinstance(result, dict) else None
            approved = _decision_is_approved(decision, None)
            future = self.pending_approval_futures.get(call_id)
            if future is not None and not future.done():
                future.set_result({"approved": approved, "decision": decision})
            return

        method = payload.get("method")
        params = payload.get("params")
        if method != "exec/approvalResponse" or not isinstance(params, dict):
            return
        call_id = params.get("call_id") or params.get("callId")
        if not isinstance(call_id, str) or not call_id.strip():
            return
        decision = params.get("decision")
        approved_hint = params.get("approved") if isinstance(params.get("approved"), bool) else None
        future = self.pending_approval_futures.get(call_id)
        if future is not None and not future.done():
            future.set_result({"approved": _decision_is_approved(decision, approved_hint), "decision": decision})

    async def _poll_inbox_once(self):
        if not self.inbox_path.exists():
            return
        inbox_size = self.inbox_path.stat().st_size
        if self.inbox_offset_bytes > inbox_size:
            self.inbox_offset_bytes = 0
        with self.inbox_path.open("rb") as inbox_file:
            inbox_file.seek(self.inbox_offset_bytes)
            while True:
                line_start = inbox_file.tell()
                raw_line = inbox_file.readline()
                if not raw_line:
                    break
                if not raw_line.endswith(b"\n"):
                    inbox_file.seek(line_start)
                    break
                try:
                    line = raw_line.decode("utf-8").strip()
                except Exception:
                    self.inbox_offset_bytes = inbox_file.tell()
                    continue
                if not line:
                    self.inbox_offset_bytes = inbox_file.tell()
                    continue
                try:
                    event = json.loads(line)
                except Exception:
                    self.inbox_offset_bytes = inbox_file.tell()
                    continue
                self.inbox_offset_bytes = inbox_file.tell()
                event_type = event.get("type")
                if event_type == "control":
                    self._record_control_decision(event.get("payload", {}))
                    continue
                if event_type != "user":
                    continue
                injection_id = str(event.get("injection_id") or "")
                content = str(event.get("content") or "")
                if not injection_id:
                    continue
                await self.pending_injections.put((injection_id, content))

    async def _injection_loop(self, sdk_symbols):
        while not self.shutdown_requested:
            try:
                injection_id, content = await asyncio.wait_for(self.pending_injections.get(), timeout=0.2)
            except asyncio.TimeoutError:
                continue
            await self._handle_user_injection(sdk_symbols, injection_id, content)

    async def run(self):
        sdk_symbols = await self._load_sdk()
        if not sdk_symbols:
            return

        def handle_shutdown(signum, frame):
            self.shutdown_requested = True

        signal.signal(signal.SIGTERM, handle_shutdown)
        signal.signal(signal.SIGINT, handle_shutdown)

        heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        injection_task = asyncio.create_task(self._injection_loop(sdk_symbols))
        self._write_heartbeat(force=True)
        try:
            while not self.shutdown_requested:
                await self._poll_inbox_once()
                await asyncio.sleep(0.1)
        finally:
            heartbeat_task.cancel()
            injection_task.cancel()
            try:
                await heartbeat_task
            except Exception:
                pass
            try:
                await injection_task
            except Exception:
                pass
            if self.client is not None:
                try:
                    await self.client.disconnect()
                except Exception:
                    pass


def main():
    asyncio.run(ClaudeWorker().run())


if __name__ == "__main__":
    main()
