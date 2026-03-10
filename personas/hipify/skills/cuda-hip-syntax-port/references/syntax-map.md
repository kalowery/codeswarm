# CUDA to HIP Syntax Map

Use this as a mechanical conversion reference, then validate behavior.

## Runtime API mappings

- `cudaMalloc` -> `hipMalloc`
- `cudaFree` -> `hipFree`
- `cudaMemcpy` -> `hipMemcpy`
- `cudaMemcpyAsync` -> `hipMemcpyAsync`
- `cudaMemset` -> `hipMemset`
- `cudaGetLastError` -> `hipGetLastError`
- `cudaPeekAtLastError` -> `hipPeekAtLastError`
- `cudaDeviceSynchronize` -> `hipDeviceSynchronize`
- `cudaStreamCreate` -> `hipStreamCreate`
- `cudaStreamDestroy` -> `hipStreamDestroy`
- `cudaEventCreate` -> `hipEventCreate`
- `cudaEventRecord` -> `hipEventRecord`
- `cudaEventSynchronize` -> `hipEventSynchronize`

## Launch and type mappings

- `dim3` and `<<<grid, block, shared, stream>>>` syntax remain source-compatible in HIP C++.
- `cudaError_t` -> `hipError_t`
- `cudaSuccess` -> `hipSuccess`
- `cudaStream_t` -> `hipStream_t`
- `cudaEvent_t` -> `hipEvent_t`

## Warp and lane considerations

- Prefer `warpSize` over hardcoded `32`.
- `__shfl_sync`/`__shfl_xor_sync` often map mechanically, but reduction logic may require changes on wave64 hardware.
- Any lane-mask logic that assumes 32 bits must be reviewed.

## Headers and compile guards

- Replace CUDA runtime include with HIP runtime include where appropriate:
  - `#include <cuda_runtime.h>` -> `#include <hip/hip_runtime.h>`
- Keep architecture-specific paths guarded:
  - CUDA path: `#if defined(__CUDA_ARCH__)`
  - ROCm path: use HIP/AMDGPU guards used by your codebase.

## Common manual-rewrite triggers

- Inline PTX (`asm("...")` with NVIDIA ISA).
- `cp.async` usage and explicit CUDA shared-memory async copy instructions.
- CUDA WMMA fragments that have no direct one-to-one ROCm implementation for the target architecture.
- Code that depends on 32-thread warp scheduling/ordering.

## Quick smoke checklist after conversion

1. Compile passes under HIP compiler.
2. Kernel launches return success.
3. Result correctness matches CUDA baseline for at least one small and one large problem size.
4. No hardcoded 32-lane assumptions remain in reductions/shuffles unless intentionally guarded.
