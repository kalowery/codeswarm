# Codeswarm Router Protocol (Overview)

Protocol ID: `codeswarm.router.v1`

Transport:

- TCP on `127.0.0.1:8765`
- JSON Lines (one JSON object per line)

This is a concise reference. See `docs/PROTOCOL_SPEC.md` for detailed payloads.

## Command envelope

```json
{
  "protocol": "codeswarm.router.v1",
  "type": "command",
  "command": "swarm_list",
  "request_id": "uuid",
  "payload": {}
}
```

Router requires `protocol` and `command`; `type` is expected by current clients.

## Event envelope

```json
{
  "protocol": "codeswarm.router.v1",
  "type": "event",
  "timestamp": "ISO-8601",
  "event": "swarm_list",
  "data": { ... }
}
```

## Supported commands

- `swarm_launch`
- `inject`
- `enqueue_inject`
- `queue_list`
- `swarm_list`
- `swarm_status`
- `approve_execution`
- `swarm_terminate`

## Core lifecycle events

- `swarm_launched`
- `inject_ack`
- `inject_delivered`
- `inject_failed`
- `turn_started`
- `assistant_delta`
- `assistant`
- `turn_complete`
- `usage`
- `queue_list`
- `queue_updated`
- `inter_swarm_enqueued`
- `inter_swarm_dispatched`
- `inter_swarm_blocked`
- `inter_swarm_dropped`
- `swarm_status`
- `swarm_terminated`
- `swarm_removed`
- `command_rejected`

## Execution/approval events

- `exec_approval_required`
- `exec_approval_resolved`
- `command_started`
- `command_completed`
- `agent_error`
- `reasoning_delta`
- `reasoning`
- `task_started`
- `task_complete`

## Backend-emitted orchestration events

These events are emitted by backend orchestration logic (not direct router normalization):

- `auto_route_submitted`
- `auto_route_ignored`

Forward compatibility rule:

- clients should ignore unknown events/fields.
