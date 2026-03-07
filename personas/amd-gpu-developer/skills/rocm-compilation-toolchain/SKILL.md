# ROCm Compilation Toolchain

Use this skill for building HIP/Triton workloads with reproducible compiler settings.

## Focus

- `hipcc` and clang flags for target architectures
- Build type, debug info, and optimization level control
- Reproducible build settings for performance comparisons

## Checklist

1. Lock compiler/toolchain versions used in measurements.
2. Set explicit architecture targets and optimization flags.
3. Build with symbols when profiling is required.
4. Capture full build commands in output notes.
