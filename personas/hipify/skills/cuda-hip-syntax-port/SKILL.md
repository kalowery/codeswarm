---
name: cuda-hip-syntax-port
description: Convert CUDA source to HIP-compatible source with minimal semantic drift. Use when a task involves `.cu`, `.cuh`, or CUDA-specific C++ code that must compile and run under ROCm/HIP, including API replacement, intrinsic translation, launch syntax conversion, and identifying CUDA-only features that require algorithmic rewrites.
---

# CUDA HIP Syntax Port

Port code in two passes: first perform mechanical syntax and API translation, then isolate CUDA-only constructs that need manual rework.

## Workflow

1. Inventory CUDA-specific constructs before editing.
2. Apply mechanical replacements from [references/syntax-map.md](references/syntax-map.md).
3. Mark unsupported or architecture-specific code paths with explicit TODO comments and runtime guards.
4. Validate that host API calls, kernel launch syntax, and device intrinsics are all HIP-compatible.

## Step 1: Inventory

Run the bundled audit script first to find common porting hotspots:

```bash
scripts/audit_cuda_constructs.sh <path>
```

Prioritize edits in this order:
1. Runtime and memory APIs (`cudaMalloc`, `cudaMemcpy`, streams/events).
2. Kernel launch and compile guards.
3. Warp intrinsics and collectives.
4. Inline PTX or CUDA-architecture-only instructions.

## Step 2: Mechanical Translation

Use mappings in [references/syntax-map.md](references/syntax-map.md):
1. Replace CUDA runtime API prefixes (`cuda*`) with HIP runtime equivalents (`hip*`) where one-to-one mappings exist.
2. Replace CUDA launch and synchronization calls with HIP runtime equivalents.
3. Replace device attributes and qualifiers only when semantics are equivalent.
4. Keep architecture guards explicit (`#if defined(__CUDA_ARCH__)` vs ROCm/GFX guards) rather than silently deleting code.

## Step 3: Intrinsics and Collectives

Translate warp-scope operations carefully:
1. Adjust assumptions around warp width (NVIDIA 32-lane warp vs common AMD 64-lane wavefront).
2. Keep mask behavior explicit when replacing `__shfl_*_sync` and ballot-style intrinsics.
3. If a kernel hardcodes `32`, convert to `warpSize`-aware logic and test reductions/scans.

## Step 4: Unsupported Features Handling

For CUDA-only features (for example `cp.async`, some inline PTX blocks, SM-specific tensor-core paths), do not force a fake mechanical translation.
1. Keep original path behind CUDA guard.
2. Add HIP path using portable memory movement and synchronization first.
3. Mark hotspot for follow-up optimization by `cuda-hip-kernel-optimization`.

## Output Contract

When finishing a porting task with this skill, return:
1. Changed files.
2. List of converted APIs/intrinsics.
3. List of unresolved CUDA-only constructs requiring manual optimization work.

## References

- [references/syntax-map.md](references/syntax-map.md)
- [scripts/audit_cuda_constructs.sh](scripts/audit_cuda_constructs.sh)
