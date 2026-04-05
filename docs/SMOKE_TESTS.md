# Smoke Tests

This document covers the smoke-test harnesses that exercise real Codeswarm workflows.

## Scope

There are two broad classes of smoke tests:

- local smokes that run entirely on the current machine
- remote smokes that provision AWS or Slurm workers and may create temporary GitHub repositories

Remote smokes are not free. They can consume cloud or cluster capacity and should be used deliberately.

## Local Smokes

### Local Orchestrated Runtime Smoke

Script: `tools/orchestrated_project_runtime_smoke.py`

Purpose:

- exercises direct-task and planner-task project execution
- supports independent planner and worker runtimes
- validates branch creation, task execution, integration, and local repo interactions

Examples:

```bash
python3 tools/orchestrated_project_runtime_smoke.py --planner-runtime mock --worker-runtime mock --mode both
python3 tools/orchestrated_project_runtime_smoke.py --planner-runtime codex --worker-runtime claude --mode both
python3 tools/orchestrated_project_runtime_smoke.py --planner-runtime claude --worker-runtime claude --mode planned
```

Key prerequisites:

- local router dependencies installed
- runtime credentials present when using real `codex` or `claude`

### Orchestrated Resume Smoke

Script: `tools/orchestrated_project_resume_smoke.py`

Purpose:

- validates project resume behavior and worker reassignment flows

Example:

```bash
python3 tools/orchestrated_project_resume_smoke.py
```

## AWS Smokes

AWS smokes isolate the config to a single AWS launch provider, start a temporary router, and terminate the swarm when finished.

### AWS Runtime Smoke

Script: `tools/aws_claude_runtime_smoke.py`

Purpose:

- validates launch, prompt injection, assistant response, and swarm teardown
- supports both native and container execution
- supports both `claude` and `codex` indirectly through provider params, though the current CLI is Claude-oriented

Common use:

```bash
python3 tools/aws_claude_runtime_smoke.py --provider aws-claude-default
python3 tools/aws_claude_runtime_smoke.py --provider aws-claude-default --execution-mode container --container-engine docker
```

Expected success signal:

- `assistant=READY`
- `termination=complete`

Key prerequisites:

- valid AWS CLI auth on the launch host
- working SSH keypair referenced by the AWS provider config
- `ANTHROPIC_API_KEY` for Claude runtime validation

### AWS Project Smoke

Script: `tools/aws_claude_project_smoke.py`

Purpose:

- validates GitHub-backed project execution on AWS
- verifies provider repo preparation, task execution, task-branch pushes, integration branch creation, and final content verification
- supports `claude` and `codex`
- supports native and container execution

Examples:

```bash
python3 tools/aws_claude_project_smoke.py --provider aws-claude-default --worker-mode claude --execution-mode container --container-engine docker
python3 tools/aws_claude_project_smoke.py --provider aws-default --worker-mode codex --execution-mode container --container-engine docker
```

Expected success signal:

- `project_status=completed`
- JSON output containing `status: ok`
- a populated `integration_branch`

Key prerequisites:

- valid AWS CLI auth on the launch host
- GitHub auth on the launch host
- `ANTHROPIC_API_KEY` for Claude runs
- `OPENAI_API_KEY` for Codex runs

Cleanup behavior:

- creates a temporary private GitHub repo under the selected owner
- deletes that repo on success or failure unless `--keep-repo` is set
- now fails the smoke if repo deletion fails

Important caveat:

- GitHub repo deletion requires `gh` auth with the `delete_repo` scope
- if that scope is missing, the smoke now fails during cleanup instead of silently leaving repos behind

## Slurm Smokes

### Slurm Runtime Smoke

Script: `tools/slurm_claude_runtime_smoke.py`

Purpose:

- validates Slurm launch, worker response, and teardown for the Claude runtime

Example:

```bash
python3 tools/slurm_claude_runtime_smoke.py --provider slurm-claude-default
```

Expected success signal:

- `assistant=READY`
- `termination=complete`

Key prerequisites:

- reachable Slurm login host
- valid cluster config in `configs/combined.json` or equivalent
- `ANTHROPIC_API_KEY`

### Slurm Project Smoke

Script: `tools/slurm_claude_project_smoke.py`

Purpose:

- validates GitHub-backed or local-path project execution on Slurm
- verifies task branches, integration branch, and final expected file contents

Example:

```bash
python3 tools/slurm_claude_project_smoke.py --provider slurm-claude-default
```

Key prerequisites:

- reachable Slurm login host
- `ANTHROPIC_API_KEY`
- GitHub auth if using GitHub repo mode

Cleanup behavior:

- creates a temporary GitHub repo in GitHub mode
- now fails the smoke if that repo cannot be deleted afterward

## Choosing The Right Smoke

Use runtime smoke when you want to validate:

- worker launch
- mailbox/follower connectivity
- prompt injection
- basic response flow

Use project smoke when you want to validate:

- repo preparation
- branch creation and pushes
- task execution
- integration branch creation
- end-to-end project behavior

## Cost And Safety

Before running AWS or Slurm smokes, verify:

- the target provider is healthy
- your credentials are loaded in the current shell
- you actually want to create cloud or cluster work right now

For GitHub-backed project smokes, verify:

- the `gh` login has repository create/delete capability
- temporary repo cleanup is working

If you intentionally want to inspect the temporary repo or router state after the run, use:

- `--keep-repo`
- `--keep-artifacts` where supported
