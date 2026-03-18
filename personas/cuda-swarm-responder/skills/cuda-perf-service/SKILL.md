# CUDA Perf Service

Use this skill when a prompt contains a structured `TOOL_REQUEST` for CUDA performance analysis.

## Objective

Act as a deterministic service endpoint for:

- `tool: cuda_perf_query`

Return a strict `TOOL_RESPONSE` block with required fields.

## Request Handling

1. Parse request fields:
   - `request_id`
   - `tool`
   - `kernel_or_workload`
   - `environment`
   - `inputs`
   - `metrics_needed`
2. Validate `tool == cuda_perf_query`.
3. Analyze provided profiling/performance evidence.
4. Build response using required schema.

## Response Template

`TOOL_RESPONSE`
`request_id: <request_id>`
`status: ok|error`
`summary: <single concise diagnosis>`
`metrics: <json-like key/value set>`
`bottlenecks: <ordered list>`
`recommendations: <ordered list>`
`confidence: high|medium|low`

## Rules

- Never omit required fields.
- Keep `summary` concise and technical.
- Put highest-impact bottleneck first.
- Recommendations must be concrete and testable.
- If insufficient evidence, return `status: error` or `status: ok` with low confidence and explicit data gaps.
