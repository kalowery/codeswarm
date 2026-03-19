# Approval Probe Harness

`tools/approval_probe.py` is a deterministic harness to test Codex approval request/response semantics directly against `codex app-server`.

It removes guesswork by empirically testing response payload variants and recording which variants actually unblock command execution (`exec_command_begin`/`exec_command_end`).

## What it does

1. Launches `codex app-server --listen stdio://`.
2. Runs `initialize` + `thread/start` + `turn/start`.
3. Waits for an approval request event:
- `codex/event/exec_approval_request`
- `item/commandExecution/requestApproval`
- `codex/event/apply_patch_approval_request`
- `item/fileChange/requestApproval`
4. Sends a selected response payload shape.
5. Observes whether command execution starts/completes.

## Run

```bash
python3 tools/approval_probe.py
```

Useful options:

```bash
python3 tools/approval_probe.py \
  --variants rpc_approved,rpc_accept,notify_accept,notify_approved,rpc_plus_notify \
  --pre-timeout 40 \
  --post-timeout 30
```

Scenario sweep (command, file-change, outside-workspace write, network):

```bash
python3 tools/approval_probe.py \
  --ask-for-approval untrusted \
  --scenarios command,filechange,filechange_strict,outside_write,network \
  --variants rpc_approved,rpc_accept,notify_accept,notify_approved,rpc_plus_notify \
  --pre-timeout 60 \
  --post-timeout 35
```

Use a stress prompt while still forcing deterministic approval traffic:

```bash
python3 tools/approval_probe.py \
  --force-escalation-prefix \
  --prompt "Randomly select a 1980s video game and implement a version that can run in a web browser." \
  --variants rpc_approved,rpc_accept,notify_accept,notify_approved,rpc_plus_notify \
  --pre-timeout 60 \
  --post-timeout 30
```

If needed:

```bash
python3 tools/approval_probe.py --codex-bin /path/to/codex
```

## Output

The script prints JSON records per variant:
- whether approval was seen
- request method and IDs
- whether command started/completed
- errors

Use this output as the ground truth for router approval fanout behavior.

## Worker-path probe (recommended)

For production-path validation (same mailbox transport as Codeswarm), use:

```bash
python3 tools/approval_worker_probe.py \
  --prompt "Randomly select a 1980s video game and implement a version that can run in a web browser." \
  --variants rpc_approved,rpc_accept,notify_accept,notify_approved,rpc_plus_notify \
  --pre-timeout 90 \
  --post-timeout 35
```

Scenario sweep via worker mailbox path:

```bash
python3 tools/approval_worker_probe.py \
  --ask-for-approval untrusted \
  --scenarios command,filechange,filechange_strict,outside_write,network \
  --variants rpc_approved,rpc_accept,notify_accept,notify_approved,rpc_plus_notify \
  --pre-timeout 90 \
  --post-timeout 35 \
  --native-wait 20
```

This probe runs `agent/codex_worker.py`, captures approval requests from outbox,
and writes control responses to inbox. It is more representative than direct
`app-server` probing when debugging Codeswarm approval stalls.

File-change approval probing:

```bash
python3 tools/approval_worker_probe.py \
  --scenario filechange_strict \
  --variants rpc_approved,notify_accept,rpc_plus_notify,rpc_abort,notify_cancel \
  --pre-timeout 90 \
  --post-timeout 35
```

## Flow Correlation Report

To inspect request->response->resume correlation for a specific run mailbox:

```bash
python3 tools/approval_flow_report.py --run-dir /path/to/run
```

Full JSON output:

```bash
python3 tools/approval_flow_report.py --run-dir /path/to/run --json
```
