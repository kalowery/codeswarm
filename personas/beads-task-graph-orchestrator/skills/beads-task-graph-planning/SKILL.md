# Beads Task Graph Planning

Use this skill to turn a software spec into a Beads task DAG and to retrieve the next ready task.

## Intent Detection

Treat prompt as `build_graph` if it includes:

- a specification
- "break down", "decompose", "create tasks", "task graph", "plan implementation"

Treat prompt as `next_task` if it includes:

- "next task"
- "what should I do next"
- "give me a task"
- "pull next"

If ambiguous, prefer `build_graph`.

## Workflow: `build_graph`

1. Parse spec into capability slices.
2. Derive implementation tasks per slice.
3. Add cross-cutting tasks (tests, integration, release/deploy checks).
4. Build DAG:
   - foundations first
   - parallelizable branches
   - integration and validation tails
5. Write tasks + dependencies into Beads repo.
6. Query ready tasks and include top 3 in summary.

### Required Task Fields

- `title`
- `description`
- `acceptance_criteria`
- `priority`
- `depends_on[]`

## Workflow: `next_task`

1. Query Beads for open tasks.
2. Filter to tasks with all dependencies completed.
3. Rank ready tasks:
   - highest priority first
   - then smallest scope
   - then oldest creation time
4. Return the single best ready task.

If none ready:

- return blocked status
- list blocking dependencies/tasks to complete first

## Response Templates

### Build Graph Response

- `status: graph_written`
- `tasks_created: <n>`
- `dependencies_created: <m>`
- `ready_tasks: [id, id, id]`
- `notes: <important assumptions>`

### Next Task Response

- `status: next_task`
- `task_id: ...`
- `title: ...`
- `objective: ...`
- `acceptance_criteria: [...]`
- `depends_on: [...]`
- `dependency_status: satisfied|blocked`

### Blocked Response

- `status: blocked`
- `reason: no tasks with satisfied dependencies`
- `blocking_tasks: [id, id, ...]`

## Guardrails

- Never assign a task that has unsatisfied dependencies.
- Never emit a "next task" that lacks acceptance criteria.
- Keep descriptions implementation-ready and verifiable.
