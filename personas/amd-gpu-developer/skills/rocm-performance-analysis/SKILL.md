# ROCm Performance Analysis

Use this skill when diagnosing GPU performance bottlenecks in HIP/Triton workloads.

## Focus

- Roofline-oriented thinking (compute vs bandwidth bound)
- Occupancy, register pressure, and memory bottlenecks
- Kernel-level and end-to-end throughput analysis

## Workflow

1. Define key metrics (latency, tokens/s, GB/s, TFLOP/s).
2. Gather baseline measurements on representative inputs.
3. Identify top bottleneck kernels and limiting resources.
4. Propose targeted, testable optimization changes.
