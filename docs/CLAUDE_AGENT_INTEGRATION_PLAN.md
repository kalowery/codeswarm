# Claude Agent Integration Plan

## Goal

Add Claude as a supported Codeswarm agent runtime so users can choose between multiple agent implementations when launching swarms, without breaking the current Codex-based workflow or the existing orchestrated project runtime.

This work should:

- preserve current Codex behavior
- keep provider/backends (`local`, `slurm`, `aws`) separate from agent runtime selection
- support both ad hoc swarms and orchestrated project workers
- preserve live observability in the existing UI
- maintain deterministic task execution semantics in project mode

## Recommendation

Claude should be integrated as a new worker runtime, not as a synthetic Codex clone.

The correct architectural boundary is:

- provider chooses where a worker runs
- worker runtime chooses which agent implementation runs inside that worker
- router consumes a provider-neutral stream of normalized worker events

Do not attempt to force Claude into fake `codex/event/*` RPC methods. Codeswarm currently assumes Codex app-server JSON-RPC at several layers, but that is an implementation detail of the existing worker, not the right long-term interface for multi-agent runtime support.

Instead:

- add a `claude_worker.py`
- define a canonical internal worker event model
- adapt Codex and Claude into that model
- keep existing Codex-specific normalization in place during migration

## Why This Is Needed

Current Codeswarm architecture is Codex-centric:

- [agent/codex_worker.py](/Users/keithlowery/codeswarm/agent/codex_worker.py) speaks stdio JSON-RPC to `codex app-server`
- [router/router.py](/Users/keithlowery/codeswarm/router/router.py) translates `codex_rpc` payloads into router/UI events
- [router/providers/local.py](/Users/keithlowery/codeswarm/router/providers/local.py) chooses `codex_worker.py` or `mock_worker.py` from `worker_mode`

That works for Codex, but it couples:

- worker transport
- worker protocol
- approval semantics
- tool lifecycle reporting
- token usage reporting

Claude support is the first real case where Codeswarm needs a runtime-neutral worker contract.

## Claude Integration Surface

The target should be Anthropic's official Claude Code embedding surface rather than a brittle CLI transcript parser.

Use a Claude runtime through an SDK/client layer that supports:

- fresh sessions and persistent sessions
- streaming assistant output
- permission callbacks / tool approvals
- tool-use visibility
- usage reporting

The worker should still own:

- mailbox polling
- heartbeat writes
- session lifecycle
- normalization into Codeswarm events

## Architecture

### 1. Separate Agent Runtime from Provider

Today launch presets effectively mix backend and runtime details. That should be split.

Recommended launch model:

- provider/backend: `local`, `slurm`, `aws`
- agent runtime: `codex`, `claude`, `mock`

Provider is about execution placement.
Agent runtime is about the software running in the worker.

This should apply consistently to:

- launch presets in config
- CLI launch flags
- web launch modal
- swarm metadata

### 2. Add a Claude Worker

Add:

- [agent/claude_worker.py](/Users/keithlowery/codeswarm/agent/claude_worker.py)

It should mirror the operational shape of [agent/codex_worker.py](/Users/keithlowery/codeswarm/agent/codex_worker.py):

- same env-driven launch contract
- same inbox/outbox JSONL mailbox flow
- same heartbeat behavior
- same provider integration contract

It should differ in the embedded runtime:

- Claude session/client initialization
- Claude message streaming
- Claude permission handling
- Claude usage extraction

### 3. Introduce a Canonical Worker Event Model

Codeswarm should have a provider-neutral, runtime-neutral set of worker events.

Canonical event families:

- `turn_started`
- `assistant_delta`
- `assistant`
- `reasoning_delta`
- `reasoning`
- `usage`
- `exec_approval_required`
- `command_started`
- `command_completed`
- `filechange_started`
- `filechange_completed`
- `task_started`
- `task_complete`
- `agent_error`
- `turn_complete`

All canonical events should include:

- `swarm_id`
- `job_id`
- `node_id`
- `injection_id`

Optional fields should carry runtime-specific detail without polluting the core contract.

### 4. Keep Raw Runtime Events as an Adapter Boundary

Short term:

- Codex worker may continue to emit `codex_rpc`
- router can continue translating Codex payloads
- Claude worker can emit already-normalized events or a `claude_rpc`/`claude_event` raw stream that is then normalized

Recommendation:

- move toward workers emitting canonical events directly
- optionally include `raw` runtime payloads for debugging

This avoids reimplementing Codex protocol assumptions for every new runtime.

## Codex vs Claude Protocol Differences

### Codex Today

Current Codex integration has:

- stdio JSON-RPC transport
- explicit `turn/start`, `turn/steer`, `thread/start`, `thread/resume`
- `codex/event/*` and `item/*` methods
- approval requests represented as RPC-like events
- explicit command/filechange begin/end events
- token usage events from `codex/event/token_count` and `thread/tokenUsage/updated`

### Claude Likely Shape

Claude integration should be assumed to provide:

- streaming assistant text/messages
- structured tool-use/tool-result events
- permission callbacks or approval hooks
- usage/cost metadata
- session-oriented client semantics rather than Codex JSON-RPC turn control

That means Claude is not a drop-in protocol match for Codex.

## Normalization Strategy

### Recommendation

Normalize Claude to Codeswarm's canonical event model, not to Codex's raw wire protocol.

This is viable because the UI and project runtime fundamentally need:

- turn lifecycle
- assistant text
- tool execution visibility
- approval lifecycle
- usage accounting
- task completion signals

Those are portable concepts.

### Likely Mappings

Claude runtime concepts should map approximately as follows:

- assistant streaming text -> `assistant_delta`
- final assistant message -> `assistant`
- session/turn start -> `turn_started`
- session/turn end -> `turn_complete`
- permission callback / tool approval -> `exec_approval_required`
- shell-like tool begin/end -> `command_started` / `command_completed`
- file edit tool begin/end -> `filechange_started` / `filechange_completed`
- runtime usage updates -> `usage`
- fatal runtime failure -> `agent_error`

If Claude exposes additional structures that do not map cleanly:

- preserve them as `raw`
- add optional canonical extension fields only if they have user value

### Recommendation on Task Signals

Codeswarm project execution should continue to rely on the existing structured `TASK_RESULT` contract rather than a model-specific completion primitive.

That keeps project orchestration deterministic across runtimes.

## Approval Model

This is the most important integration risk.

Current Codeswarm approval logic in [router/router.py](/Users/keithlowery/codeswarm/router/router.py) is heavily tuned to Codex event shapes and decision dialects.

Claude integration should not reuse that logic by pretending to be Codex.

Instead:

- define a canonical approval request record
- define canonical approval decisions
- add runtime-specific translation at the worker edge

Canonical decisions should likely be:

- `approve`
- `approve_for_session`
- `deny`
- `abort`

Then:

- Codex worker translates canonical decisions into Codex-native response payloads
- Claude worker translates canonical decisions into Claude-native permission responses

This avoids continued growth of router logic that is keyed on runtime-specific event names.

## Usage and Spend Accounting

Codeswarm already has project/task/worker usage accounting in router.

Claude support should feed the same normalized usage fields:

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
- `usage_source`

If Claude exposes richer cost metadata:

- keep it as supplemental runtime-specific metadata
- do not replace the canonical token accounting model

That keeps UI cost/spend reporting runtime-neutral.

## UI Recommendation

The current launch modal should remain unified.

Changes:

- add an agent runtime selector
- keep provider preset selection
- show runtime-specific advanced fields conditionally
- surface runtime type in swarm cards/details

For project creation and resume flows:

- planner swarm and worker swarms should carry runtime metadata
- users should be able to mix runtimes only if explicitly allowed

Recommendation for MVP:

- require a single runtime per swarm
- allow mixed swarms across the system
- do not allow mixed runtimes inside a single launched swarm

Recommendation for orchestrated projects:

- planner and worker swarms may be different runtimes
- router should treat them uniformly if they satisfy the task contract

## Config Changes

### Launch Provider Schema

Extend launch/provider defaults to include runtime selection explicitly.

Current:

- `worker_mode`

Recommended:

- `agent_runtime`
- runtime-specific fields grouped by runtime

Suggested migration:

- preserve `worker_mode` as a backward-compatible alias
- internally map `worker_mode=codex|mock` to `agent_runtime`
- add `claude`
- phase out `worker_mode` once the CLI/UI/config schema are updated

### Worker Runtime Fields

Claude-specific launch fields will likely include:

- `claude_model`
- `claude_permission_mode`
- `claude_fresh_session_per_injection`
- `claude_allowed_tools`
- `claude_max_turn_duration_seconds`
- `claude_env_profile` or auth profile selector if needed

## Provider Changes

Provider changes should be minimal.

Local provider:

- choose `codex_worker.py`, `claude_worker.py`, or `mock_worker.py`
- pass runtime-specific env vars

Slurm/AWS providers:

- same runtime switch in launch scripts
- package/install Claude runtime prerequisites
- ensure remote auth/environment injection is supported

Provider behavior should remain runtime-agnostic beyond selecting which worker executable to launch.

## Router Changes

### Phase 1 Router Work

- introduce a runtime-neutral event normalization boundary
- preserve current Codex path
- accept Claude worker events and normalize them
- store `agent_runtime` in swarm metadata

### Phase 2 Router Work

- refactor approval state away from Codex-specific method matching
- move to canonical approval records and decisions
- keep runtime-specific reply synthesis in worker/runtime adapters

### Phase 3 Router Work

- factor task/tool lifecycle normalization into reusable helper modules
- reduce monolithic runtime-specific logic in [router/router.py](/Users/keithlowery/codeswarm/router/router.py)

## Orchestrated Project Recommendation

Claude should be fully usable in orchestrated projects, but only if the worker contract remains model-independent.

That means:

- task prompts remain plain text
- repo/workspace preparation remains provider-owned
- completion remains `TASK_RESULT`
- follow-up task proposal format remains router/planner owned

Do not depend on Claude-specific planning or subagent features for orchestrated project correctness.

Recommendation:

- keep Codeswarm router as the orchestration authority
- do not let Claude manage its own hidden subagent swarm for project execution

Nested orchestration would make:

- spend attribution
- live observability
- task ownership
- quiescence detection

much harder to reason about.

## Risks

### 1. Approval Semantics Drift

Claude permission handling may not align 1:1 with Codex approval request/response flow.

Mitigation:

- move approvals to a canonical internal model
- keep runtime translation in the worker layer

### 2. Tool Lifecycle Visibility Gaps

Claude may expose tool use in a different granularity than Codex command/filechange begin/end events.

Mitigation:

- normalize what maps cleanly
- add a generic tool event if needed
- do not block MVP on exact parity for every tool subtype

### 3. Usage Accounting Differences

Claude usage events may arrive at different times or with different cumulative/incremental semantics.

Mitigation:

- normalize to the existing canonical token fields
- prefer incremental usage when available
- add regression tests for cross-runtime project usage accounting

### 4. Remote Runtime Packaging

Slurm/AWS workers may need additional runtime installation or authentication setup.

Mitigation:

- ship local-provider MVP first
- make remote provider enablement phase 2

### 5. Router Complexity Growth

If Claude is bolted directly into the current Codex-specific router logic, router complexity will grow quickly.

Mitigation:

- add a runtime-neutral adapter boundary before full Claude rollout

## Recommended Phase Plan

### Phase 0: Research Spike

- prototype a standalone Claude worker
- capture raw event/message/permission/usage behavior
- document exact runtime mapping gaps
- decide whether direct SDK integration or CLI wrapper is the production path

Deliverable:

- `docs/CLAUDE_RUNTIME_SPIKE.md`

### Phase 1: Runtime Selection Plumbing

- add `agent_runtime` to launch metadata
- support `codex`, `claude`, `mock`
- update local provider launch path
- add UI runtime selector
- add swarm metadata display for runtime

Deliverable:

- launch a local Claude-backed swarm from web and CLI

### Phase 2: Claude Worker MVP

- implement `claude_worker.py`
- support mailbox-driven prompt injection
- stream assistant output
- emit canonical events
- support fresh session per injection
- support persistent session mode

Deliverable:

- ad hoc Claude swarm usable in UI

### Phase 3: Approval and Tool Mapping

- map Claude permission callbacks to canonical approvals
- map tool begin/end lifecycle into canonical command/filechange events
- ensure approval UI works end to end

Deliverable:

- Claude swarm can execute with approval workflow intact

### Phase 4: Usage, Billing, and Observability

- normalize usage into router spend accounting
- verify per-turn, per-worker, per-project accounting
- surface runtime-specific metadata where useful in the UI

Deliverable:

- Claude usage visible in the same spend dashboards as Codex

### Phase 5: Orchestrated Project Support

- verify `TASK_RESULT` contract under Claude workers
- run deterministic project-task execution with Claude workers
- validate planner-runtime combinations:
  - Codex planner + Claude workers
  - Claude planner + Codex workers
  - Claude planner + Claude workers

Deliverable:

- Claude usable as planner or worker in project mode

### Phase 6: Remote Provider Support

- enable Claude runtime under Slurm and AWS launch scripts
- document auth/bootstrap requirements
- add smoke coverage for remote runtime launch paths where feasible

Deliverable:

- Claude runtime available across all supported providers

## Testing Plan

### Unit Tests

- runtime event normalization fixtures
- approval translation logic
- usage normalization and delta accounting
- task completion parsing with Claude workers

### Integration Tests

- local Claude worker launch
- streamed assistant output
- approval flow round trip
- command/filechange observability
- persistent-session and fresh-session behavior

### Browser Tests

- launch modal runtime selection
- runtime-specific field rendering
- live transcript visibility
- approval visibility for Claude workers

### Project Tests

- direct-task project with Claude worker swarm
- planned project with Claude planner swarm
- mixed planner/worker runtime compatibility
- resume behavior with Claude task branches

## Recommendation Summary

The right design is:

- Claude as a new worker runtime
- provider-neutral canonical worker events
- runtime-specific worker adapters
- router as orchestration authority
- shared UI/runtime concepts across Codex and Claude

The wrong design is:

- pretending Claude speaks Codex JSON-RPC
- embedding Claude-specific orchestration inside the worker runtime
- coupling project correctness to a model-specific event stream

## Immediate Next Slice

If implementation begins, start with:

1. add `agent_runtime` to swarm launch metadata and config normalization
2. add local-provider runtime switch for `claude_worker.py`
3. define the canonical event schema in router documentation
4. build a Claude worker spike that can stream assistant text and usage from one injection

That gives a controlled first step without forcing a full router approval refactor up front.
