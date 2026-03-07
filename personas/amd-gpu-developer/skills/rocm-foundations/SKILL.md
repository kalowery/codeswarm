# ROCm Foundations

Use this skill when configuring or validating AMD GPU software environments.

## Focus

- ROCm runtime and driver compatibility
- Device visibility and topology
- Version alignment across compiler/runtime libraries

## Checklist

1. Confirm ROCm stack versions (`rocminfo`, `rocm-smi`, `hipcc --version`).
2. Verify visible GPUs and expected architecture targets.
3. Validate runtime behavior with a minimal HIP smoke test.
4. Record environment details used for benchmark results.
