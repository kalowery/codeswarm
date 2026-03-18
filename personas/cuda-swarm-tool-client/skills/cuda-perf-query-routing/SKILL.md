# CUDA Perf Query Routing

Use this skill to invoke CUDA performance analysis as a routed tool call to swarm alias `CUDA`.

## Tool Definition

- Tool: `cuda_perf_query`
- Endpoint: `/swarm[CUDA]/idle/reply`
- Return path: automatic via `/reply`

## When To Use

- You need CUDA kernel/workload profiling interpretation.
- You need performance diagnosis from CUDA/Nsight outputs.
- You need concrete tuning recommendations tied to measured metrics.

## Invocation Template

Send exactly:

`/swarm[CUDA]/idle/reply`
`TOOL_REQUEST`
`request_id: <short-id>`
`tool: cuda_perf_query`
`kernel_or_workload: <name>`
`environment: <gpu/cuda/framework>`
`inputs: <commands, traces, snippets, or measurements>`
`metrics_needed: <comma-separated metrics>`
`output_schema: summary, metrics, bottlenecks, recommendations, confidence`

## Validation Checklist

Accept result only if all are present:

- `TOOL_RESPONSE`
- matching `request_id`
- `status`
- `summary`
- `metrics`
- `bottlenecks`
- `recommendations`
- `confidence`

If missing fields:

1. Request one corrected `TOOL_RESPONSE` with same `request_id`.
2. If still invalid, continue with best-effort local reasoning and state uncertainty.

## Integration Pattern

After receiving valid response:

1. Cite key metrics.
2. Apply top 1-3 recommendations to current task.
3. State what additional data would improve confidence.
