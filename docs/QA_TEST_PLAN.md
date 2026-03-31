# Codeswarm QA Test Plan

## 1. Purpose

Define a practical, staged test strategy for Codeswarm covering:

- Control-plane correctness (router protocol, state, queueing)
- Product behavior (CLI, backend API/WebSocket, frontend flows)
- Provider behavior (local and Slurm)
- HPC execution quality on AMD Instinct + ROCm clusters

This plan is optimized for the current repository state, which now includes targeted automated smoke coverage for orchestrated projects and a headless browser suite for the web UI.

## 1.1 Current Automated Coverage

Implemented repository-level automation now includes:

- Python compile smoke for router and worker code
- TypeScript compile/build coverage for CLI and web packages
- headless browser UI tests via Puppeteer:
  - `node --test tools/web_ui/browser.test.cjs`
- orchestrated project resume smoke:
  - `python3 tools/orchestrated_project_resume_smoke.py`

These suites exercise project creation, worker interaction, resume preview, blocked-resume handling, and end-to-end project completion behavior in the current web stack.

## 2. Scope

### In Scope

- Router command/event protocol (`codeswarm.router.v1`)
- Swarm lifecycle: launch, inject, queue, approval, terminate
- Local provider mailbox and worker orchestration
- Slurm provider SSH/Slurm interactions
- Backend event translation, persistence, and auto-routing
- Frontend critical user flows tied to real backend/router events
- ROCm workload execution through Codeswarm-managed workers on Instinct nodes

### Out of Scope (initial phase)

- Deep model-level quality benchmarking (LLM answer quality)
- Kernel-level ROCm debugging in vendor drivers
- Cross-cluster federation beyond one router instance

## 3. Quality Risks (Highest First)

1. Event ordering and state drift between router, backend, and frontend (multi-threaded router + async bridge).
2. Inter-swarm queue correctness under concurrency (`idle` dispatch, blocked/dropped behavior).
3. Approval flow correctness for synthetic tool-call approvals and router control messages.
4. Slurm/SSH failure handling (timeouts, partial failures, stale job state).
5. ROCm environment mismatch on cluster nodes (driver/runtime/library skew) causing false app failures.
6. Persistence/restart consistency (`router_state.json`, `web/backend/state.json`).

## 4. Test Environments

### Environment Matrix

| Env | Purpose | Backend | Infra |
|---|---|---|---|
| E1 Local Dev | Fast protocol and UX iteration | `local` | Single host |
| E2 Slurm Staging | Scheduler/SSH semantics | `slurm` | Non-production Slurm partition |
| E3 Slurm ROCm | Real GPU validation | `slurm` | AMD Instinct nodes (e.g., MI300X/MI250) |

### ROCm Baseline Checklist (E3)

- `rocminfo` and `rocm-smi` executable on compute nodes.
- Stable ROCm stack version pinned per release cycle.
- Framework stack pinned for tests (example: Python + PyTorch ROCm wheel version).
- Node health pre-check: GPU visibility, ECC health, XGMI/link status, free memory headroom.

## 5. Test Levels and Coverage

### L0: Static and Build Gates (all PRs)

- Python syntax check: `python3 -m py_compile` for `router/`, `agent/`, `slurm/`, `common/`.
- TypeScript compile for CLI and backend/frontend.
- Frontend lint.
- Config schema smoke (valid/invalid JSON fixtures through `common/config.py`).

Pass Criteria:
- Zero compile/lint errors.

### L1: Unit Tests (new)

Target modules:

- `common/config.py`
  - Required/optional keys per backend.
  - Error messages for missing fields and unsupported backend.
- `web/backend/src/server.ts` parsing helpers
  - Node selector parsing (`/node[...]`)
  - Cross-swarm directive parsing (`idle`, `first-idle`, `all`, `node[...]`)
- `web/backend/src/state/SwarmStateManager.ts`
  - Alias uniqueness, persistence atomicity, remove/update behavior.

Pass Criteria:
- >= 90% line coverage on tested modules.

### L2: Integration Tests (local backend)

Spin up router + backend with `configs/local.json`; drive protocol via TCP and backend HTTP/WebSocket.

Required scenarios:

1. `swarm_launch` emits `swarm_launched`; prompt auto-inject emits `inject_ack` and delivery result.
2. `inject` to single node, range, and `all`; verify inbox JSONL payload shape.
3. Inter-swarm queue:
  - enqueue idle work
  - dispatch to first idle
  - blocked case on inject failure
  - dropped case when target swarm terminated
4. Approval flow:
  - `exec_approval_required` surfaced to backend
  - `/approval` call returns `exec_approval_resolved`
5. Termination and cleanup:
  - `swarm_terminate` + router/backend state convergence.
6. Orchestrated project runtime:
  - project create/start through router/backend
  - deterministic task dispatch to worker swarms
  - project resume with replacement worker swarm
  - resume preview blocked/unblocked paths

Pass Criteria:
- 100% pass on required scenarios.
- No leaked worker processes after suite completion.

### L3: Integration Tests (Slurm provider with mock SSH)

Mock `subprocess.run` SSH/Slurm responses for deterministic coverage of:

- `launch()` job id parsing success/failure.
- `get_job_state()` timeout and empty state.
- `list_active_jobs()` parse robustness for malformed lines.
- `inject()`/`send_control()` remote command failure surfaces as error.

Pass Criteria:
- All provider error paths covered.

### L4: End-to-End Tests (real Slurm + Instinct/ROCm)

Daily smoke in E3:

1. Launch 1-node and N-node swarms on Instinct partition.
2. Submit prompt that executes ROCm probe command:
   - `rocminfo`
   - `rocm-smi`
3. Submit prompt that runs a minimal framework workload (example):
   - PyTorch ROCm tensor allocation + single matmul.
4. Validate outbox event stream continuity (`turn_started` -> `assistant_delta`/`assistant` -> `turn_complete`/`task_complete`).
5. Terminate swarms and verify no orphan Slurm jobs.

Weekly extended:

1. Multi-swarm routing chain using `/swarm[alias]/idle`.
2. Approval-required command flow in cluster environment.
3. Restart router during active swarm; verify reconciliation.
4. Induce remote SSH interruption and verify non-crashing behavior.

Pass Criteria:
- Smoke: 100% pass per run.
- Extended: no critical failures for 2 consecutive weekly runs before release.

## 6. Non-Functional Tests

### Performance/Scale

- Launch latency: `swarm_launch` request to `swarm_launched`.
- Injection latency: `inject_ack` to `inject_delivered`.
- Queue dispatch latency for `idle` selector under N concurrent items.
- Router steady-state memory and CPU with long-running follower stream.

Targets (initial):
- P95 launch latency: define per-cluster baseline and alert on > 25% regression.
- P95 inject delivery latency: baseline + 25%.
- No unbounded memory growth in 2-hour soak.

### Resilience

- Kill/restart router mid-run; verify state reconciliation and continued event flow.
- Simulate malformed JSONL outbox lines; verify parser resilience.
- Force stale backend state file; verify startup recovery behavior.

### Security/Hardening

- Protocol fuzzing for unknown command/payload types (`command_rejected` expected).
- Validate no command injection in Slurm provider remote command construction.
- Validate approval decisions are scoped to known `(job_id, call_id)`.

## 7. Test Data and Fixtures

- Minimal local test fixtures:
  - valid/invalid config files
  - canned router events
  - sample outbox JSONL streams
- Slurm mock fixtures:
  - `squeue`, `sbatch`, `scancel`, SSH stderr/stdout variants
- ROCm smoke payload templates:
  - shell snippets for `rocminfo`, `rocm-smi`, framework probe

## 8. CI/CD Gating Strategy

### Required on every PR

1. L0 static/build gates.
2. L1 unit tests.
3. L2 local integration tests.
4. Headless browser UI suite for core web flows.

### Required on merge to `main`

1. All PR gates.
2. L3 Slurm-provider mock integration.

### Scheduled (nightly/weekly)

1. Nightly: L4 E3 smoke (ROCm/Instinct).
2. Weekly: L4 extended + resilience tests.

Release gate:
- Block release if any nightly smoke failure remains unresolved > 24h.

## 9. Execution Plan (First 4 Weeks)

### Week 1

- Add test harnesses (Python `pytest`, Node test runner for backend helpers).
- Implement L1 tests for config and backend parsing/state.
- Add CI workflows for L0 + L1.

### Week 2

- Implement L2 local integration suite (router/backend launch, inject, terminate).
- Add process cleanup and log artifact capture.

### Week 3

- Implement L3 Slurm-provider mock tests.
- Add deterministic fixtures for Slurm command outputs.

### Week 4

- Stand up E3 nightly ROCm smoke pipeline on Instinct partition.
- Capture baseline metrics (latency, failure rate, stability).

## 10. Exit Criteria for “Test Plan Implemented”

This plan is considered implemented when:

1. L0-L2 run in CI on every PR.
2. L3 runs on merge to `main`.
3. Nightly L4 ROCm smoke is active with alerting.
4. Release checklist references these gates and blocks on failures.
