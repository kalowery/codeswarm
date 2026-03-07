# HIP Kernel Development

Use this skill for writing and tuning HIP kernels on AMD GPUs.

## Focus

- Grid/block decomposition and occupancy tradeoffs
- Global/shared/register memory behavior
- Divergence minimization and coalesced access

## Workflow

1. Establish a correctness baseline versus CPU/reference implementation.
2. Implement kernel with explicit boundary checks and deterministic test inputs.
3. Tune launch dimensions and memory access patterns.
4. Measure throughput/latency and validate numerical parity.
