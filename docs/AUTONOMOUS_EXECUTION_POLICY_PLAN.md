# Autonomous Execution Policy Plan

## Goal

Maximize worker autonomy in orchestrated mode without giving agents broad authority to modify arbitrary parts of the filesystem, destroy repo state, or bypass human oversight for high-risk actions.

The core approach is:

- hard isolation at the workspace boundary
- deterministic router-enforced execution policy
- task-scoped automatic approvals
- permanently gated high-risk actions

This should apply to the coordinated project mode and remain opt-in, so current uncoordinated swarm behavior can continue to exist.

## Design Principles

1. Autonomy should come from policy, not trust in the model.
2. Workers should be free to act within a bounded task scope.
3. The router should remain the enforcement point for execution policy.
4. The filesystem boundary should be hard, not advisory.
5. Merge authority should be separate from implementation authority.
6. All automatic approvals should be auditable.

## Recommended Model

### 1. Hard Workspace Isolation

Each worker should operate inside its own isolated clone or worktree.

Allowed:

- read and write inside the assigned workspace root
- create task branches in the assigned repo clone
- run local build, test, and lint commands inside the workspace

Disallowed by default:

- writes outside the workspace root
- global shell profile edits
- home directory mutation
- global package installation
- arbitrary destructive filesystem operations

This provides the primary blast-radius boundary.

### 2. Router-Generated Task Policy

For orchestrated mode, the router should derive an execution policy for each task from:

- `project_id`
- `task_id`
- `repo_path`
- `workspace_root`
- `workspace_subdir`
- `owned_paths`
- `expected_touch_paths`
- assigned branch name

The router should then convert that task metadata into a concrete allow/deny policy used when evaluating worker tool requests.

This makes approvals deterministic and tied to task structure instead of agent judgment.

### 3. Auto-Approved Safe Operations

The system should automatically approve these classes of actions when they remain inside the worker workspace:

- reading files in the workspace
- listing/searching files in the workspace
- editing files under task-owned or expected-touch paths
- creating new files under task-owned or expected-touch paths
- running repo-local build, test, lint, format, and typecheck commands
- normal git workflow commands:
  - `git status`
  - `git diff`
  - `git checkout -b <task-branch>`
  - `git add`
  - `git commit`
  - `git push origin <task-branch>`

These are the operations that should not require constant human intervention in coordinated mode.

### 4. Restricted Deletes

Delete operations should not be broadly allowed, but some deletes are necessary for autonomous work.

Auto-approve deletes only when the target is:

- inside the worker workspace, and
- under task-owned paths, or
- inside explicitly safe transient directories such as:
  - `dist/`
  - `build/`
  - `.next/`
  - `target/`
  - cache/temp directories created by the task

Still require approval or deny outright for:

- deletes outside the workspace
- deletes at repo root with broad globbing
- branch deletion on shared refs
- commands equivalent to mass cleanup of unknown scope

### 5. Permanently Gated High-Risk Actions

These actions should never be generally auto-approved in orchestrated mode:

- writes outside the assigned workspace
- force push
- push to `main` or other protected branches
- `git reset --hard`
- `git clean -fdx`
- deleting shared branches
- deleting repos
- changing credentials or secrets
- global installs or machine-level configuration changes
- arbitrary networked side effects unrelated to the task

These should either be denied or require explicit human approval.

### 6. Role-Specific Policy Profiles

Use different autonomy profiles depending on agent role.

Planner profile:

- read-only repo access
- no file mutation
- no push authority

Worker profile:

- autonomous within task scope
- branch creation, file edits, commit, push-to-task-branch allowed
- no protected branch writes

Merger profile:

- may inspect multiple task branches
- may prepare integration branch or PR
- merge remains subject to branch protection and repo policy

Uncoordinated profile:

- preserves current behavior
- no forced migration to orchestrated policy

### 7. Credential Scope

Agent autonomy should also be constrained by credential design.

Recommended:

- use repo-scoped or org-approved credentials
- allow pushing task branches and creating PRs
- do not allow repo deletion
- do not allow org administration
- do not allow bypass of protected branch rules

In practice, task workers should usually have enough GitHub authority to:

- clone the assigned repo
- push a task branch
- open or update a PR

But not enough to:

- delete the repository
- alter org settings
- merge protected branches directly

### 8. Merge as a Separate Authority

Workers should not be the final authority for merging changes.

Preferred flow:

1. worker completes task
2. worker pushes branch or opens PR
3. checks run
4. controller or merger agent decides integration action

This preserves high implementation autonomy while reducing risk from a single worker making irreversible repo-level decisions.

### 9. Path Ownership as the Main Scope Control

Task metadata should drive the policy boundary.

If a task owns:

- `src/foo/**`

then the worker can freely edit within that scope.

If the worker needs to modify files outside the owned scope, the preferred behavior is:

- emit a structured follow-up task, or
- request escalation with explicit justification

This makes task decomposition more important, but it gives the system a deterministic basis for autonomy.

### 10. Auditability

Every auto-approved action should be logged with:

- project ID
- task ID
- swarm ID
- node ID
- workspace root
- command
- approval basis
- matched path scope
- timestamp

This gives operators a reliable audit trail without requiring interactive approvals for routine progress.

## Recommended Enforcement Strategy

The router should be the authority for execution-policy decisions.

At minimum, the router should:

1. classify each requested command
2. resolve effective workspace boundaries
3. compare file targets against task-owned scope
4. auto-approve if the rule matches a safe policy
5. reject or escalate if the rule falls outside policy
6. emit policy-decision events for UI and audit storage

This should not depend on prompt wording or the worker “behaving correctly.”

## Suggested Implementation Phases

### Phase 1: Policy Data Model

Add task policy fields to orchestrated project state:

- `policy_profile`
- `allowed_write_paths`
- `allowed_delete_paths`
- `allowed_command_classes`
- `disallowed_command_classes`
- `push_target`

Derive these automatically from task metadata where possible.

### Phase 2: Router Enforcement

Implement command classification and allow/deny logic in the router approval path.

Targets:

- shell commands
- file edits
- patch application
- git operations

### Phase 3: UI Visibility

Expose, per task:

- autonomy profile
- policy scope
- auto-approved actions
- blocked/escalated attempts

This is important so users can understand why a worker is progressing freely or getting stopped.

### Phase 4: Credential Binding

Bind execution profile to credential profile.

Example:

- worker swarms get branch-push credentials only
- merger swarms get PR/integration permissions

### Phase 5: Follow-Up Task Emission

When a worker encounters out-of-scope work, prefer:

- `needs_followups`
- structured task proposals

instead of broadening write permissions mid-task.

## Recommendation

The best way to maximize autonomy safely is to:

- give workers highly permissive access inside a narrow workspace and task path scope
- give them enough repo authority to push task branches
- keep protected branches, destructive operations, and out-of-scope filesystem actions gated
- make the router, not the model, the source of truth for what is allowed

That gives the system high throughput without effectively handing agents unrestricted machine access.
