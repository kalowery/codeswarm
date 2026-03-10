#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 <path>"
  exit 1
fi

target="$1"

if [[ ! -d "$target" && ! -f "$target" ]]; then
  echo "Path does not exist: $target"
  exit 1
fi

echo "== CUDA/HIP porting hotspot audit: $target =="

patterns=(
  "cuda[A-Za-z0-9_]+\\("
  "__global__|__device__|__host__"
  "__shfl(_xor|_down|_up)?(_sync)?\\("
  "cooperative_groups"
  "wmma|mma\\.sync|ldmatrix|cp\\.async"
  "__CUDA_ARCH__|CUDART_VERSION|__CUDACC__"
  "asm\\s+volatile\\("
)

for p in "${patterns[@]}"; do
  echo
  echo "-- pattern: $p"
  rg -n --no-heading "$p" "$target" || true
done
