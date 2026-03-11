# Routed Delegation

Use this skill to treat `/swarm[...].../reply` as a tool invocation in codeswarm.

## When To Use

- A subtask is better handled by another specialized swarm/persona.
- You need parallel decomposition with clear request/response contracts.
- You need deterministic handoff and structured return payloads.

## Request Construction

1. Choose target:
   - Deterministic: `/swarm[alias]/node[n]/reply`
   - Flexible: `/swarm[alias]/idle/reply`
   - Fanout: `/swarm[alias]/all/reply`
2. Use this body:
   - `TOOL_REQUEST`
   - `request_id: ...`
   - `tool: ...`
   - `goal: ...`
   - `inputs: ...`
   - `constraints: ...`
   - `output_schema: ...`
   - `timeout_hint: ...`

## Validation

Accept only responses shaped as:

- `TOOL_RESPONSE`
- `request_id` matching your original request
- `status: ok|error`
- `result` matching requested schema

If validation fails:

1. Ask the same target for correction once.
2. If it still fails, perform local fallback and continue.

## Practical Guidance

- Keep delegated goals narrow; avoid open-ended prompts.
- Specify exactly what format you need in `output_schema`.
- Include hard constraints explicitly (ports, paths, runtime limits, safety rules).
- Record delegated evidence before applying results.

