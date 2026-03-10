# CUTLASS to Composable Kernel Pattern Map

Use this file when translating CUDA kernel optimization ideas into HIP/ROCm equivalents.

## 1) Multistage async pipeline mapping

- CUTLASS explicitly models SM80 `cp.async` staging and grouping:
  - `cutlass/include/cute/arch/copy_sm80.hpp` lines 45-67 (`cp.async`) and 161-184 (`commit_group`, `wait_group`).
  - `cutlass/include/cutlass/gemm/threadblock/mma_multistage.h` lines 137-155 and 294-316 (async copy iterations and staged copy loop).
- CK models pipelined LDS prefetch/read/write/GEMM sequencing:
  - `composable_kernel/include/ck/tensor_operation/gpu/grid/gridwise_gemm_pipeline_v1.hpp` lines 158-223.

Porting rule:
1. Preserve producer/consumer overlap intent.
2. Replace CUDA async-copy microcode with CK pipeline/prefetch controls.
3. Tune prefetch stages (`NumPrefetch`) and verify occupancy.

## 2) Warp vs wavefront scheduling

- CUTLASS hardcodes warp constants (`NumThreadsPerWarp = 32`):
  - `cutlass/include/cutlass/cutlass.h` lines 96-101.
- CK exposes scheduler choices for wavefront behavior:
  - `composable_kernel/include/ck/utility/scheduler_enum.hpp` lines 32-44 (`Intrawave`, `Interwave`).
  - `composable_kernel/include/ck/tensor_operation/gpu/grid/gridwise_gemm_pipeline_v1.hpp` lines 750-767 (interwave selector and note).

Porting rule:
1. Rewrite warp-lane assumptions to wave-aware logic.
2. Evaluate `Intrawave` and `Interwave` scheduling as a tuning dimension, not a fixed translation.

## 3) Tensor operation path mapping

- CUTLASS chooses operator class and architecture-specific tensor-op kernels (`OpClassTensorOp`, architecture tags).
- CK routes through XDL/MFMA/WMMA-capable templates and architecture macros:
  - `composable_kernel/include/ck/ck.hpp` lines 105-118 (MFMA feature macros).
  - `composable_kernel/include/ck/wrapper/operations/gemm.hpp` (XDL wrappers and per-wave MFMA concepts).

Porting rule:
1. Preserve math intent (tile MMA shape and accumulation type), not instruction mnemonics.
2. Select CK instance family aligned with target AMD architecture.

## 4) Shared memory vs LDS synchronization shape

- CUTLASS multistage GEMM uses circular stage buffers and iterator advancement:
  - `cutlass/include/cutlass/gemm/threadblock/mma_multistage.h` lines 248-285.
- CK pipelines place explicit `block_sync_lds()` around copy/compute phases:
  - `composable_kernel/include/ck/tensor_operation/gpu/grid/gridwise_gemm_pipeline_v1.hpp` lines 195-223 and 743-746.

Porting rule:
1. Preserve synchronization correctness first.
2. Then reduce redundant barriers by selecting CK pipeline variant/scheduler that already embeds needed sync.

## 5) Vectorized memory access constraints

- CUTLASS chooses cache behavior by alignment and access width:
  - `cutlass/include/cutlass/gemm/threadblock/default_mma_multistage_blockwise.h` lines 165-173.
- CK validates vector access dimensions and scalar-per-vector constraints at runtime argument checks:
  - `composable_kernel/include/ck/tensor_operation/gpu/device/impl/device_contraction_multiple_abd_xdl_cshuffle.hpp` lines 640-705.

Porting rule:
1. Recompute vector widths from ROCm alignment/stride reality.
2. Do not carry CUDA vector widths blindly; satisfy CK validity predicates.

## Practical tuning checklist

1. Match compute data types and accumulation semantics first.
2. Choose CK pipeline version and scheduler.
3. Tune tile sizes and vector widths to pass CK validity checks.
4. Confirm correctness across edge shapes (tails/unaligned dimensions).
5. Benchmark and iterate scheduler + prefetch + vector width together.
