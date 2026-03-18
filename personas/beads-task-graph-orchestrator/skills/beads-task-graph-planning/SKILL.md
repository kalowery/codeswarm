# Beads Task Graph Planning

Use this skill to turn a software spec into a Beads task DAG and to retrieve the next ready task.

## Mandatory Preflight

Run this first in the target workspace:

```bash
BEADS_BIN="$(command -v bd || command -v beads || true)"
test -n "$BEADS_BIN"
"$BEADS_BIN" --version
"$BEADS_BIN" --help
```

Then discover supported subcommands for this installed version:

```bash
"$BEADS_BIN" create --help
"$BEADS_BIN" dep --help
"$BEADS_BIN" ready --help
"$BEADS_BIN" show --help
"$BEADS_BIN" list --help
```

Do not assume command names. Use what help output confirms.

Markdown-first policy:

- For `build_graph`, markdown-based graph creation is required by default.
- Fall back to per-task CLI creation only if markdown import/input is unsupported in the installed `bd` version.

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
5. Write tasks + dependencies into Beads repo using markdown input/import by default.
6. Query ready tasks and include top 3 in summary.

### Canonical Markdown-First Patterns (verified against `bd --help`)

Use markdown creation first:

```bash
# create multiple issues from markdown
"$BEADS_BIN" create -f <tasks.md> --json
```

Dependency links in markdown should be expressed so `bd create -f` can materialize them directly.
If some dependency edges are not represented by markdown parsing, add them explicitly after creation:

```bash
# blocked depends on blocker
"$BEADS_BIN" dep add <blocked-id> <blocker-id> --json
# equivalent form:
"$BEADS_BIN" dep <blocker-id> --blocks <blocked-id> --json
```

### Per-Task Fallback Patterns (only if markdown import is unavailable)

Use these only as fallback if supported by `--help`:

```bash
# create task
"$BEADS_BIN" create --title "<title>" --description "<description>" --priority "<priority>" --json

# set acceptance criteria (repeat or pass as supported)
"$BEADS_BIN" update <task_id> --acceptance "<criterion>" --json

# add dependency edge: child depends on parent
"$BEADS_BIN" dep add <child_id> <parent_id> --json
```

If these exact subcommands are unavailable, map to equivalent discovered commands and record the mapping in output notes.

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

### Canonical Query Patterns (adapt to installed CLI)

```bash
# list truly ready work (blocker-aware)
"$BEADS_BIN" ready --json

# inspect selected task including dependencies/details
"$BEADS_BIN" show <task_id> --json
```

If JSON output is unavailable, parse stable text output conservatively and note that limitation.

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
- Include `commands_run` in every response so execution is auditable and reproducible.
