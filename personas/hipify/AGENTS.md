# AGENTS.md

## Purpose

This workspace is for CUDA-to-HIP porting with performance parity goals.  
Primary source repositories:

- `cutlass/` (CUDA reference patterns)
- `composable_kernel/` (ROCm/HIP optimization patterns)

## Skills

### Available skills

- `cuda-hip-syntax-port`: Mechanical CUDA-to-HIP translation for APIs, launch syntax, intrinsics, and compile guards.  
  Path: `/Users/klowery/hipify/skills/cuda-hip-syntax-port/SKILL.md`
- `cuda-hip-kernel-optimization`: Performance-focused mapping from CUTLASS-style CUDA optimizations to CK/ROCm equivalents.  
  Path: `/Users/klowery/hipify/skills/cuda-hip-kernel-optimization/SKILL.md`

## Skill Triggering Rules

Use `cuda-hip-syntax-port` when the request includes:

- Converting CUDA source (`.cu`, `.cuh`, CUDA C++) to HIP.
- Replacing CUDA runtime APIs with HIP runtime APIs.
- Porting CUDA intrinsics and kernel launch semantics.

Use `cuda-hip-kernel-optimization` when the request includes:

- Recovering or improving performance after mechanical HIP conversion.
- Mapping CUDA multistage pipeline/tiling/tensor-op strategy to ROCm.
- Retuning for wavefront scheduling, LDS behavior, and vectorized memory access.

Use both skills in this order for full kernel ports:

1. `cuda-hip-syntax-port`
2. `cuda-hip-kernel-optimization`

## Porting Expectations

1. Preserve numerical correctness first.
2. Keep CUDA-only optimized paths guarded when no safe HIP equivalent exists yet.
3. Make architecture assumptions explicit (warp vs wavefront, tensor-op path).
4. Report unresolved CUDA-only constructs and optimization follow-ups.
