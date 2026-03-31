# Orchestrated Projects Plan

## Implementation Status

This document began as a forward plan. The repository now implements the core orchestration/runtime path plus the first resume UX wave:

- router-backed project/task runtime
- planner-driven project planning path
- deterministic worker dispatch
- per-worker isolated repository preparation
- automatic final integration task
- web UI project/task visibility
- project resume and resume preview
- CLI support for project resume

Open higher-level follow-on work still includes:

- worker-generated follow-up task proposal normalization
- richer project policy controls
- cost/latency-aware assignment
- automated worker provisioning during resume

## Goal

Add an opt-in Codeswarm capability for deterministic coordination of multiple agents working on the same software project without replacing the current uncoordinated swarm model.

The new capability introduces an **orchestrated project** mode:

- A planner agent can turn a spec plus repo reference into a task graph.
- The router becomes the runtime authority for task assignment and completion.
- Workers receive one concrete task at a time, operate in isolated repo workspaces, and return structured results.
- Users can observe both project-level progress and the live worker transcript in the existing UI.

## Architecture

### 1. Planning Layer

Use the existing Beads task-graph planner persona to:

- decompose a software spec into a dependency-aware DAG
- ensure each task has an implementation-ready prompt
- persist the graph in Beads for auditability and human inspection

Beads remains the task graph and planning surface, but not the runtime scheduler.

### 2. Runtime Orchestration Layer

Add a router-managed project/task runtime overlay that tracks:

- project identity and repo reference
- worker pool swarms
- task dependency state
- atomic claims/assignments
- attempts and retries
- structured task results
- quiescence

The router is the source of truth for execution state because it already owns durable control-plane state, worker idle detection, and queue dispatch.

### 3. Workspace Isolation Layer

Agents must not share the same mutable checkout.

Recommended model:

- local/shared-host execution: per-agent clone or worktree seeded from a source repo
- remote execution: per-task or per-agent clone from a cached source
- each task runs on a dedicated branch

Phase 1 lands a local-provider MVP that prepares a per-agent repo clone under each worker workspace.

### 4. Delivery Layer

Prompt routing stays in the system, but as a transport primitive instead of the scheduler.

- Current `/swarm[...]` routing remains available for ad hoc work.
- Orchestrated projects dispatch via router-owned project scheduling.
- Auto-parsed free-form worker chaining is not the runtime authority for project progress.

### 5. UI Layer

The UI remains unified.

- Keep the existing swarm/node transcript experience.
- Add a project/task view above it.
- Link each task to its assigned swarm/node so users can inspect the live worker output.

## Data Model

### Project

- `project_id`
- `title`
- `repo_path`
- `base_branch`
- `worker_swarm_ids[]`
- `status`
- `created_at`
- `updated_at`
- `workspace_subdir`
- `tasks{}`

### Task

- `task_id`
- `title`
- `prompt`
- `acceptance_criteria[]`
- `depends_on[]`
- `owned_paths[]` or `expected_touch_paths[]`
- `status`
- `attempts`
- `assigned_swarm_id`
- `assigned_node_id`
- `assignment_injection_id`
- `branch`
- `result_status`
- `result_raw`
- `last_error`
- `created_at`
- `updated_at`

## Structured Worker Contract

Workers must return a structured result block so the router can deterministically advance execution.

Canonical shape:

```text
TASK_RESULT
task_id: T-104
status: done|blocked|failed|needs_followups
branch: codeswarm/project/T-104
base_commit: abc1234
head_commit: def5678
files_changed:
- path/a
- path/b
verification:
- npm test -- ...
notes: ...
```

If the structured result is missing or malformed, the router should not silently treat the task as successful.

## Why Beads + Router

This should be a hybrid model:

- **Beads**: graph authoring, dependency intent, auditability
- **Router**: deterministic execution, claims, leases, assignment, retries, restart safety

Prompt routing alone is too weak because it lacks durable task claims and completion semantics.

## Project Resume

Projects should be resumable after router restarts, host failures, or proactive worker-swarm termination.

The resume source of truth should be:

- router project/task state for scheduling metadata
- the canonical project repository for durable branch verification

Resume should not depend on old worker workspaces. Those are disposable.

### Resume Rules

- If a task has a durable task branch and the branch still exists, the router may keep it completed.
- If a task was `assigned` when the system stopped, resume should either:
  - recover it as completed from the task branch when durable branch evidence exists, or
  - reset it to `pending` when no durable branch evidence exists.
- If a task previously failed, resume may optionally retry it by moving it back to `pending`.
- If a completed task branch is missing, the task must be downgraded to `pending`.
- If a completed dependency is downgraded, downstream completed tasks, including the integration task, must also be reset.

### Initial Resume Scope

The first implementation slice should:

- add a router `project_resume` command
- allow replacing the worker swarm set during resume
- reverify completed tasks against the canonical repo
- reset stale assignments from dead swarms
- reuse the existing deterministic project dispatcher after reconciliation

This keeps resume deterministic without changing the existing ad hoc swarm workflow.

## Phase Plan

### Phase 1

Land the orchestration MVP:

- router project/task state
- project creation, listing, and start commands
- deterministic ready-task scheduling to idle worker nodes
- structured `TASK_RESULT` parsing
- local-provider per-agent repo clone preparation
- backend API exposure
- frontend project visibility linked to live swarm transcripts

### Phase 2

Add task-graph growth and planner integration:

- worker-generated follow-up task proposals
- router-side proposal staging
- planner/Beads normalization path
- unresolved-proposal-aware quiescence

### Phase 3

Add integration automation:

- branch verification
- PR creation or merge queue integration
- dependency-aware downstream refresh
- path-lock-aware scheduling

### Phase 4

Add advanced orchestration:

- multiple worker pools by persona/provider
- cost/latency aware assignment
- replanning after repeated failures
- richer project controls

### Phase 5

Add richer resume UX:

- resume preview in the UI
- per-task resume reasons
- explicit retry policies for failed tasks
- CLI support for project resume

## Phase 1 Scope

This repository change set focuses on:

- an opt-in router-backed orchestrated project model
- durable project/task state in router persistence
- local-provider repo preparation for same-repo worker execution
- read/write backend APIs for projects
- initial frontend project observability with direct access to live worker transcripts

Planner-driven Beads graph generation remains part of the target architecture, but this repository now includes both the runtime/project foundation and the initial planning/resume UX required to execute those graphs deterministically once supplied.
