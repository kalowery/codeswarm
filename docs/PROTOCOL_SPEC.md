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
  "system_prompt": "..."
}
```

Result event:

- `swarm_launched`

Data fields:

- `request_id`
- `swarm_id`
- `job_id`
- `node_count`

Notes:

- provider-specific launch parameters come from config, not command payload.
- router immediately injects `system_prompt` to each node.

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
      "status": "running|terminated",
      "backend": "local|slurm",
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
- `status` (`running` or `terminated`)

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
{ "swarm_id": "..." }
```

Result event: `swarm_terminated`

Data:

- `request_id`
- `swarm_id`

## 4. Worker event normalization

Router consumes worker outbox `codex_rpc` messages and emits normalized events.

### 4.1 Conversation events

- `turn/started` -> `turn_started`
- `codex/event/agent_message_content_delta` -> `assistant_delta`
- `codex/event/agent_message` -> `assistant`
- `turn/completed` -> `turn_complete`
- token usage updates -> `usage`

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

