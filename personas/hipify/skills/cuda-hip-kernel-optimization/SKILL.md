---
name: cuda-hip-kernel-optimization
description: Map CUDA kernel optimization strategies to HIP/ROCm implementations with performance-focused rewrites. Use when a CUDA kernel has already been translated (or partially translated) and needs optimization parity on AMD GPUs, especially for tiling, pipelining, shared-memory/LDS staging, warp-to-wavefront adaptation, vectorized loads/stores, and tensor-op path selection.
---

# CUDA HIP Kernel Optimization

Use this skill after mechanical porting to recover performance and architecture fit on ROCm.

## Workflow

1. Extract the CUDA kernel optimization intent (tile sizes, stages, tensor op path, memory movement).
2. Map each CUDA optimization mechanism to a ROCm/CK equivalent.
3. Re-tune for wavefront and vectorization constraints.
4. Re-validate correctness and performance.

## Step 1: Identify CUDA Intent

Capture these parameters from CUDA code:
1. Threadblock/warp/instruction tile hierarchy.
2. Pipeline stage count and async copy model.
3. Shared-memory layout and synchronization pattern.
4. Warp-level reductions/collectives.

## Step 2: Map to CK/ROCm Building Blocks

Use [references/cutlass-ck-patterns.md](references/cutlass-ck-patterns.md) to map:
1. CUDA multistage `cp.async` pipelines -> CK blockwise pipelines with prefetch/scheduling choices.
2. CUDA warp-centric logic -> wave-aware logic (`warpSize`-aware or explicit wave assumptions).
3. CUDA tensor op specialization -> CK XDL/MFMA or WMMA/XDL variants based on target architecture.
4. CUDA vectorized global/shared transfers -> CK vector-load/store constraints.

## Step 3: Re-tune for Wavefront and Memory

Prioritize:
1. Wave mapping: ensure reductions/shuffles do not assume 32 lanes.
2. LDS scheduling: evaluate `Intrawave` vs `Interwave`.
3. Pipeline buffering: tune prefetch/stage depth.
4. Vector width/alignment: satisfy CK vector access validity constraints.

## Step 4: Guardrails

1. Do not mimic CUDA inline PTX behavior with unstable hacks; use native ROCm constructs.
2. Keep architecture-specific specializations explicit and selectable.
3. Preserve a correctness-first fallback path when introducing aggressive optimizations.

## Output Contract

When finishing an optimization task with this skill, return:
1. The CUDA optimization mechanism identified.
2. The HIP/CK equivalent chosen.
3. Tuning parameters changed (tile/stage/scheduler/vector width).
4. Performance result and correctness status.

## References

- [references/cutlass-ck-patterns.md](references/cutlass-ck-patterns.md)
