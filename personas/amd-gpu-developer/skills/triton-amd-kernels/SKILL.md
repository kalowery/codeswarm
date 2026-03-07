# Triton AMD Kernels

Use this skill when implementing or optimizing Triton kernels targeting ROCm backends.

## Focus

- Block size/meta-parameter tuning
- Memory layout, vectorization, and masking behavior
- Mapping Triton abstractions to AMD execution characteristics

## Workflow

1. Start from a simple correct Triton kernel and reference test.
2. Tune tile shapes and num-warps/num-stages style parameters.
3. Profile kernel-level bottlenecks and memory behavior.
4. Keep benchmark harnesses stable across tuning iterations.
