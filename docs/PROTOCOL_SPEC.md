# Codeswarm Router Protocol Specification

Version: `codeswarm.router.v1`

## 1. Transport and framing

- Router listens on `127.0.0.1:8765`
- UTF-8 JSON Lines over TCP
- one JSON object per `\n`

## 2. Message envelopes

### Command (client -> router)

```json
{
  "protocol": "codeswarm.router.v1",
  "type": "command",
  "command": "<string>",
  "request_id": "<string>",
  "payload": { ... }
}
```

### Event (router -> client)

```json
{
  "protocol": "codeswarm.router.v1",
  "type": "event",
  "timestamp": "<ISO8601 UTC>",
  "event": "<string>",
  "data": { ... }
}
```

## 3. Command set

### 3.1 `swarm_launch`

Payload:

```json
{
  "nodes": 4,
  "system_prompt": "...",
  "agents_md_content": "optional markdown string",
  "agents_bundle": {
    "mode": "file | directory",
    "agents_md_content": "optional markdown string",
    "skills_files": [
      { "path": "tooling/SKILL.md", "content": "..." }
    ]
  },
  "provider": "optional provider id",
  "provider_params": {
    "provider-specific": "values"
  }
}
```

Result event:

- `swarm_launched`

Data fields:

- `request_id`
- `swarm_id`
- `job_id`
- `node_count`
- `provider` (backend id used for this swarm)
- `provider_id` (launch provider preset id)

Notes:

- router immediately injects `system_prompt` to each node.
- `agents_bundle.mode = "directory"` represents Agent Persona payloads:
  - `agents_md_content` is copied as `AGENTS.md`
  - `skills_files` paths are copied under `.agents/skills/<path>`
  - empty `skills_files` is valid (skills optional)

### 3.1a `providers_list`

Payload:

```json
{}
```

Result event: `providers_list`

Data:

```json
{
  "request_id": "...",
  "providers": [
    {
      "id": "slurm-a100",
      "label": "Slurm A100",
      "backend": "slurm",
      "defaults": {
        "partition": "a100",
        "time_limit": "01:00:00"
      },
      "launch_soft_timeout_seconds": 900,
      "launch_hard_timeout_seconds": 2700,
      "launch_fields": [
        {
          "key": "partition",
          "label": "Partition",
          "type": "text"
        }
      ]
    }
  ]
}
```

### 3.2 `inject`

Payload:

```json
{
  "swarm_id": "...",
  "nodes": "all" | 0 | [0,1],
  "content": "..."
}
```

Lifecycle events per target node:

1. `inject_ack`
2. `inject_delivered` or `inject_failed`

### 3.3 `swarm_list`

Payload:

```json
{}
```

Result event: `swarm_list`

Data:

```json
{
  "request_id": "...",
  "swarms": {
    "<swarm_id>": {
      "job_id": "...",
      "node_count": 1,
      "system_prompt": "...",
      "status": "running|terminating|terminated",
      "provider": "local|slurm",
      "provider_id": "provider preset id",
      "terminated_at": 0
    }
  }
}
```

### 3.4 `swarm_status`

Payload:

```json
{ "swarm_id": "..." }
```

Result event: `swarm_status`

Data (success):

- `request_id`
- `swarm_id`
- `job_id`
- `node_count`
- `status` (`running`, `terminating`, or `terminated`)

Data (error path):

- `request_id`
- `swarm_id`
- `error`

### 3.5 `approve_execution`

Payload:

```json
{
  "job_id": "...",
  "call_id": "...",
  "approved": true,
  "decision": "approved|abort|accept|cancel|..."
}
```

`decision` may also be an object carrying execution-policy amendment, e.g.:

```json
{
  "approved_execpolicy_amendment": {
    "proposed_execpolicy_amendment": ["..."]
  }
}
```

or

```json
{
  "acceptWithExecpolicyAmendment": {
    "execpolicy_amendment": ["..."]
  }
}
```

Result event:

- `exec_approval_resolved`

Data:

- `request_id`
- `job_id`
- `call_id`
- `approved`
- `decision`

If `(job_id, call_id)` is unknown, router emits `command_rejected`.

### 3.6 `swarm_terminate`

Payload:

```json
{
  "swarm_id": "...",
  "terminate_params": {
    "download_workspaces_on_shutdown": true
  }
}
```

Result events:

- `swarm_status` with `status: "terminating"`
- `swarm_terminated`
- optional `workspace_archive_ready`
- optional `workspace_archive_failed`

Data:

- `request_id`
- `swarm_id`

Notes:

- Repeat terminate requests while termination is already in progress are treated idempotently; router emits `swarm_status` (`terminating`) again.
- `swarm_removed` is a separate cleanup/pruning event and is not the primary success signal for terminate.
- `workspace_archive_ready` includes `archive_path` and `archive_name`.

### 3.7 `enqueue_inject`

Payload:

```json
{
  "source_swarm_id": "...",
  "target_swarm_id": "...",
  "selector": "idle|all|nodes",
  "nodes": [0, 2],
  "content": "..."
}
```

Behavior:

- `selector = "idle"`: enqueue prompt and dispatch to first idle node in target swarm.
- `selector = "all"`: immediate fanout to all nodes in target swarm.
- `selector = "nodes"`: immediate inject to provided node list.

Result events:

- `inter_swarm_enqueued`
- `inter_swarm_dispatched`
- `inter_swarm_blocked`
- `inter_swarm_dropped`

### 3.8 `queue_list`

Payload:

```json
{}
```

Result event:

- `queue_list`
- `queue_updated` (broadcast snapshot update)

Data:

```json
{
  "request_id": "...",
  "items": [
    {
      "queue_id": "...",
      "request_id": "...",
      "source_swarm_id": "...",
      "target_swarm_id": "...",
      "selector": "idle",
      "content": "...",
      "created_at": 0
    }
  ]
}
```

`queue_updated` carries the same `items` shape and is emitted whenever queue state changes.

## 4. Worker event normalization

Router consumes worker outbox `codex_rpc` messages and emits normalized events.

### 4.1 Conversation events

- `turn/started` -> `turn_started`
- `codex/event/agent_message_content_delta` -> `assistant_delta`
- `codex/event/agent_message` -> `assistant`
- `turn/completed` -> `turn_complete`
- token usage updates -> `usage`
- `thread/status/changed` -> `thread_status`

`usage` payload includes normalized token metrics:

- `total_tokens`
- `input_tokens`
- `cached_input_tokens`
- `output_tokens`
- `reasoning_output_tokens`
- `last_total_tokens`
- `last_input_tokens`
- `last_cached_input_tokens`
- `last_output_tokens`
- `last_reasoning_output_tokens`
- `model_context_window`
- `usage_source`

### 4.2 Reasoning and task events

- `codex/event/agent_reasoning_delta` -> `reasoning_delta`
- `codex/event/agent_reasoning` -> `reasoning`
- `codex/event/task_started` -> `task_started`
- `codex/event/task_complete` -> `task_complete`

### 4.3 Command execution and approval

- `codex/event/exec_approval_request`
- `item/commandExecution/requestApproval`

both normalize to `exec_approval_required` with fields such as:

- `call_id`
- `command`
- `reason`
- `cwd`
- `proposed_execpolicy_amendment`
- `available_decisions`

Router also caches approval metadata to route later `approve_execution` control messages.

Other execution events:

- `codex/event/exec_command_begin` -> `command_started`
- `codex/event/exec_command_end` -> `command_completed`

### 4.4 Error events

- `codex/event/error` -> `agent_error`
- `error` -> `agent_error`

## 5. Common data fields in runtime events

Most translated runtime events include:

- `swarm_id`
- `job_id`
- `node_id`
- `injection_id`

`turn_started` may also include:

- `prompt` (backend-correlated injected prompt text)

## 6. Error handling

Router emits `command_rejected` for invalid command targets or processing errors.

Example:

```json
{
  "event": "command_rejected",
  "data": {
    "request_id": "...",
    "reason": "unknown swarm_id"
  }
}
```

## 7. Compatibility guidance

- clients must ignore unknown fields and events
- protocol string changes for breaking revisions

## 8. Backend Auto-Routing From Task Completion

Backend now inspects `task_complete` final assistant output and auto-submits any
line-level directives matching:

- `/swarm[alias]/idle ...`
- `/swarm[alias]/idle/reply ...`
- `/swarm[alias]/first-idle ...`
- `/swarm[alias]/all ...`
- `/swarm[alias]/node[0,2-4] ...`

Behavior:

- each directive line becomes a new router command (`enqueue_inject` for idle, `inject` for all/nodes)
- processing is deduplicated per `injection_id` to avoid duplicate dispatch on repeated events
- unknown target aliases are ignored and emitted to clients as `auto_route_ignored`
- successful submissions are emitted as `auto_route_submitted`
- when `/reply` is present, backend tracks request/injection correlation and emits a return inject to the originating node on destination `task_complete` (`auto_reply_submitted` / `auto_reply_ignored`)

These are backend-emitted UI events, not router-native protocol events.
