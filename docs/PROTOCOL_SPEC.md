# Codeswarm Router Protocol Specification

Version: `codeswarm.router.v1`  
Date: 2026‑02‑25

---

# 1. Overview

`codeswarm.router.v1` defines the JSON-line control protocol between:

- Client (CLI, Web UI, OpenClaw adapter)
- Router (Python daemon)

Transport:

- TCP
- Bound to `127.0.0.1:8765`
- JSON Lines (one JSON object per line, newline-delimited)

The router is the authoritative control-plane boundary.

---

# 2. Transport Layer

## 2.1 Connection

- Client initiates TCP connection.
- Router accepts multiple concurrent clients.
- Each connection is independent.
- Router broadcasts events to all connected clients.

## 2.2 Framing

- UTF‑8 encoded
- One JSON object per line
- Delimited by `\n`
- No multi-line JSON
- Partial frames must be buffered by client

Example:

```
{"protocol":"codeswarm.router.v1",...}\n
{"protocol":"codeswarm.router.v1",...}\n
```

---

# 3. Envelope Structure

All protocol messages must contain:

```json
{
  "protocol": "codeswarm.router.v1",
  ...
}
```

Invalid or missing protocol fields are ignored.

---

# 4. Command Messages (Client → Router)

Structure:

```json
{
  "protocol": "codeswarm.router.v1",
  "command": "<string>",
  "request_id": "<uuid>",
  "payload": { ... }
}
```

## 4.1 Fields

| Field        | Type   | Required | Description |
|-------------|--------|----------|------------|
| protocol    | string | yes      | Must equal `codeswarm.router.v1` |
| command     | string | yes      | Command name |
| request_id  | string | yes      | Client-generated UUID |
| payload     | object | yes      | Command-specific data |

---

# 5. Event Messages (Router → Client)

Structure:

```json
{
  "protocol": "codeswarm.router.v1",
  "type": "event",
  "timestamp": "<ISO8601>",
  "event": "<event_name>",
  "data": { ... }
}
```

## 5.1 Fields

| Field     | Type   | Description |
|----------|--------|------------|
| protocol | string | Version |
| type     | string | Always `"event"` |
| timestamp| string | ISO8601 UTC |
| event    | string | Event name |
| data     | object | Event payload |

---

# 6. Commands

---

## 6.1 swarm_launch

### Request

```json
{
  "protocol": "codeswarm.router.v1",
  "command": "swarm_launch",
  "request_id": "uuid",
  "payload": {
    "nodes": 4,
    "partition": "mi2508x",
    "time": "00:10:00",
    "account": "optional",
    "qos": "optional",
    "system_prompt": "..."
  }
}
```

### Response Event

```json
{
  "event": "swarm_launched",
  "data": {
    "request_id": "...",
    "swarm_id": "...",
    "job_id": "...",
    "node_count": 4,
    "partition": "mi2508x",
    "time": "00:10:00"
  }
}
```

### Errors

```json
{
  "event": "command_rejected",
  "data": {
    "request_id": "...",
    "reason": "partition is required"
  }
}
```

---

## 6.2 inject

### Request

```json
{
  "protocol": "codeswarm.router.v1",
  "command": "inject",
  "request_id": "uuid",
  "payload": {
    "swarm_id": "...",
    "nodes": "all",
    "content": "..."
  }
}
```

`nodes` may be:

- `"all"`
- integer
- list of integers

### Event Sequence

1. `inject_ack`
2. `inject_delivered` OR `inject_failed`

Example:

```json
{
  "event": "inject_ack",
  "data": {
    "request_id": "...",
    "swarm_id": "...",
    "injection_id": "...",
    "node_id": 0
  }
}
```

---

## 6.3 swarm_list

### Request

```json
{
  "command": "swarm_list",
  ...
}
```

### Response

```json
{
  "event": "swarm_list",
  "data": {
    "request_id": "...",
    "swarms": { ... }
  }
}
```

---

## 6.4 swarm_status

### Request

```json
{
  "command": "swarm_status",
  "payload": {
    "swarm_id": "..."
  }
}
```

### Response

```json
{
  "event": "swarm_status",
  "data": {
    "request_id": "...",
    "swarm_id": "...",
    "job_id": "...",
    "node_count": 4,
    "status": "running",
    "slurm_state": "RUNNING"
  }
}
```

---

## 6.5 swarm_terminate

### Request

```json
{
  "command": "swarm_terminate",
  "payload": {
    "swarm_id": "..."
  }
}
```

### Response

```json
{
  "event": "swarm_terminated",
  "data": {
    "request_id": "...",
    "swarm_id": "..."
  }
}
```

---

# 7. Streaming Runtime Events

These events originate from worker nodes.

All include:

- `swarm_id`
- `job_id`
- `node_id`
- `injection_id`

---

## 7.1 turn_started

```json
{
  "event": "turn_started",
  "data": { ... }
}
```

---

## 7.2 assistant_delta

Streaming partial token output:

```json
{
  "event": "assistant_delta",
  "data": {
    "content": "partial text"
  }
}
```

---

## 7.3 assistant

Final message:

```json
{
  "event": "assistant",
  "data": {
    "content": "full message"
  }
}
```

---

## 7.4 turn_complete

Signals completion of a turn.

---

## 7.5 usage

Token accounting:

```json
{
  "event": "usage",
  "data": {
    "total_tokens": 1234
  }
}
```

---

# 8. Concurrency Guarantees

- Router never blocks main loop.
- Slurm calls run in background threads.
- Injection runs asynchronously.
- Follower streaming is non-blocking.
- TCP clients may connect/disconnect at any time.

---

# 9. Error Handling

- Invalid JSON → ignored.
- Invalid protocol → ignored.
- Unknown swarm → `command_rejected`.
- SSH failure → `inject_failed`.
- Slurm query timeout → timeout-based response.

---

# 10. Stability Guarantees

- Router restart safe via `router_state.json`.
- Slurm reconciliation on startup.
- No reliance on stdio.
- Deterministic TCP framing.
- Versioned protocol boundary.

---

# 11. Future Compatibility

Future versions must:

- Change `protocol` string.
- Maintain backward compatibility layer if needed.
- Avoid breaking envelope structure.
