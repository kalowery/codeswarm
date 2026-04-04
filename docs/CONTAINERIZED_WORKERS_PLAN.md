# Containerized Workers Plan

## Goal

Add optional containerized worker execution to existing Codeswarm providers so agent runtimes can run either:

- natively on the host
- inside a container launched by a provider-selected engine

This should improve environment reproducibility and dependency isolation without forcing Codeswarm to adopt a separate scheduler such as Kubernetes.

## Recommendation

Do **not** start with a dedicated `k8s` provider.

Start by extending existing providers (`local`, `aws`, `slurm`) with optional container execution.

Reasoning:

- provider selection already answers "where does compute come from?"
- containers answer "how is the worker process packaged and isolated?"
- Codeswarm already has provider-specific logic for launch, repo prep, archiving, and recovery
- adding `k8s` now would duplicate scheduler/lifecycle logic before we know whether container support alone is sufficient

Add a `k8s` provider later only if we need Kubernetes-native scheduling and storage semantics:

- pod/job lifecycle as the primary execution model
- PVC / secret / service-account integration
- namespace-level multitenancy
- cluster-autoscaler and nodepool scheduling
- direct pod log/event integration

## Design Boundary

Split the concepts clearly:

- provider/backend: `local`, `aws`, `slurm`
- worker runtime: `codex`, `claude`, `mock`
- execution packaging: `native`, `container`
- container engine: `docker`, `podman`, `apptainer`, later maybe `k8s`

The provider remains responsible for:

- obtaining compute
- staging repo/mailbox/runtime assets
- launching the worker process
- injection/control delivery
- archive/export behavior

The execution mode remains responsible for:

- how the worker process is actually started
- what filesystem mounts it sees
- what runtime dependencies are preinstalled in the environment

## Proposed Config Model

### Launch/provider params

Add new optional launch fields shared across providers:

- `execution_mode`: `native | container`
- `container_engine`: engine name accepted by the provider
- `container_image`: worker image reference
- `container_pull_policy`: `always | if_not_present | never`
- `container_workdir`: optional override inside the container
- `container_args`: optional extra engine args
- `container_mount_mode`: `bind | shared_fs`
- `container_env_profile`: optional provider-defined env bundle for container launch

Keep defaults:

- `execution_mode=native`
- provider-specific default engine if `execution_mode=container`

### Provider config

Each backend should be able to declare supported engines and defaults.

Example shape:

```json
{
  "cluster": {
    "slurm": {
      "default_execution_mode": "container",
      "default_container_engine": "apptainer",
      "supported_container_engines": ["apptainer"],
      "container_images": {
        "claude-default": "oras://registry.example/codeswarm/claude-worker:latest",
        "codex-default": "docker://ghcr.io/kalowery/codeswarm/codex-worker:latest"
      }
    }
  }
}
```

Important:

- `container_engine` must be configurable per provider and per launch
- `slurm` must explicitly support `apptainer`
- `aws` and `local` should prefer `docker` or `podman` depending on host capability

## Provider Capability Matrix

### Local

Initial engines:

- `docker`
- optionally `podman`

Local container launch can bind:

- worker workspace
- mailbox root
- tool/runtime cache if needed

### AWS

Initial engines:

- `docker`
- optionally `podman`

AWS can either:

- install the engine on bootstrapped hosts
- or require AMIs that already contain the engine

Do not mix this with ECS/EKS yet. The AWS provider should still allocate EC2 and then run containers on those nodes.

### Slurm

Initial engine:

- `apptainer`

Optional later:

- `docker` only where cluster policy allows it

Slurm is the strongest reason to make `container_engine` configurable. Many HPC systems:

- forbid Docker daemon usage
- allow `apptainer` / `singularity`
- require images to be pulled or converted in specific ways

So Slurm must not assume Docker semantics.

## Launch Abstraction

Introduce an internal execution-launch layer inside providers.

Recommended shape:

- provider resolves worker runtime and launch metadata
- provider resolves execution mode
- provider delegates to a runtime-specific launcher:
  - native codex
  - native claude
  - container codex
  - container claude

This should likely become a small helper abstraction rather than more `if worker_mode == ...` branching directly inside each provider.

Example concepts:

- `_build_worker_env(...)`
- `_build_native_worker_command(...)`
- `_build_container_worker_command(...)`
- `_container_mounts_for_job(...)`
- `_ensure_container_engine_ready(...)`

## Container Contract

Containerized workers should preserve the existing mailbox protocol and worker env contract.

Required env passed into the container:

- `CODESWARM_JOB_ID`
- `CODESWARM_NODE_ID`
- `CODESWARM_BASE_DIR`
- runtime-specific fields such as:
  - `CODESWARM_ASK_FOR_APPROVAL`
  - `CODESWARM_CLAUDE_PERMISSION_MODE`
  - `CODESWARM_CLAUDE_MODEL`
  - `CODESWARM_CODEX_BIN` only if still needed

Required mounted paths:

- run workspace for that agent
- mailbox root
- optional shared repo source path for project mode
- optional runtime/tool cache

The worker inside the container must still:

- read the same inbox JSONL
- write the same outbox JSONL
- emit the same heartbeat file

If that contract stays stable, the router and frontend should not care whether the worker is native or containerized.

## Container Image Strategy

Do not try to build images dynamically during launch.

Preferred strategy:

- publish versioned worker base images
- select them by provider/runtime
- allow launch-time override via `container_image`

Suggested image split:

- `codeswarm-codex-worker`
- `codeswarm-claude-worker`

Each image should include:

- Python runtime
- worker script dependencies
- runtime-specific dependencies
  - `claude-agent-sdk` for Claude
  - Codex prerequisites for Codex

Avoid baking repo contents into images. Repos remain mounted/staged at runtime.

## Engine-Specific Notes

### Docker / Podman

Common pattern:

- `docker run --rm ...`
- bind mount workspace/mailbox paths
- pass env values explicitly
- set working directory to the prepared agent repo or agent root

### Apptainer

Common pattern:

- `apptainer exec --bind ... image.sif python3 /path/to/worker.py`

Important differences from Docker:

- often runs rootless by design
- image source may be `docker://...` or a prebuilt `.sif`
- bind syntax and writable behavior differ
- cluster admins may restrict network/image caching locations

So Slurm container support should include:

- configurable image reference
- optional pre-pulled local image path
- configurable cache/tmp directories if the cluster requires them

## Project Mode Requirements

Container support is only credible if project-mode works.

Requirements:

- prepared repo path must be mounted into the container as the working tree
- branch creation/commit/push must happen inside that mounted repo
- provider-managed Git credentials must be available inside the container

That means each provider needs a provider-specific credential strategy:

- local: host git config or injected token/ssh agent
- aws: current token/env-file approach can be mounted/injected
- slurm: `apptainer` path should expose the staged credential material safely to the container

Do not rely on global host git config from inside containers unless explicitly configured.

## Security Model

Containerization is not a complete sandbox by itself.

We should treat it as:

- reproducibility and dependency isolation first
- optional security improvement second

Rules:

- do not pass raw secrets on process command lines
- prefer staged env files or engine env-file support
- scope mounts narrowly per worker
- do not mount the whole host workspace if only per-agent paths are needed
- keep approval semantics unchanged at the worker layer

For `apptainer`, verify whether env propagation and bind paths leak more host context than intended.

## UI / Config Surface

Launch modal updates should be modest:

- `Execution Mode`
- `Container Engine`
- `Container Image`
- maybe advanced section for:
  - pull policy
  - extra args
  - mount mode

Do not expose every engine flag in the first UI pass.
Prefer a small stable set plus provider config defaults.

## Implementation Phases

### Phase 1: Shared abstraction

- add config schema for `execution_mode` and `container_engine`
- add launch fields to provider factory
- add provider capability/default resolution helpers
- no behavior change when `execution_mode=native`

### Phase 2: Local container workers

- implement `docker`-based local worker launch
- support both Codex and Claude
- verify direct swarm path
- verify project mode with local repo prep

This is the safest place to refine mount/env behavior.

### Phase 3: AWS container workers

- add engine bootstrap for EC2 hosts
- add container worker launch commands
- preserve current repo prep path
- verify direct swarm and project mode

### Phase 4: Slurm apptainer workers

- add `apptainer` engine support to Slurm provider/allocation path
- stage images or support `docker://` image refs
- verify mailbox access, repo mounts, and token staging
- verify project mode on a real Slurm cluster

### Phase 5: Optional k8s provider evaluation

Only after phases 1-4:

- evaluate whether Kubernetes-specific scheduling/storage features justify a new provider

## Testing Plan

Unit tests:

- launch fields per provider
- config/default resolution for `execution_mode` and `container_engine`
- engine-specific command rendering
- secret handling for env files / engine env injection
- project mount path rendering

Smoke tests:

- local containerized Claude runtime
- local containerized Codex runtime
- AWS containerized runtime
- Slurm `apptainer` runtime
- project smoke for each provider/runtime combination that is supported

Live validation focus:

- mailbox compatibility
- repo edits visible on mounted worktree
- branch push/integration behavior
- termination cleanup

## Open Questions

- Should `container_engine` be selectable independently from `execution_mode`, or only when `execution_mode=container`?
- Do we want one generic worker image with both Codex and Claude dependencies, or smaller runtime-specific images?
- For Slurm/apptainer, should we support both `.sif` paths and `docker://` image refs in the first cut?
- Should provider configs be able to forbid some engines entirely even if the UI knows about them?

## Recommendation Summary

The right first move is:

1. keep existing providers
2. add optional container execution to them
3. make `container_engine` explicit and provider-specific
4. treat Slurm `apptainer` support as a first-class target, not an afterthought
5. defer a dedicated `k8s` provider until container support in existing providers proves insufficient
