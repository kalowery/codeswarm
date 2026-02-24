#!/bin/bash
#SBATCH --job-name=codex-alloc
#SBATCH --output=codex_alloc_%j.out
#SBATCH --error=codex_alloc_%j.err

# This job simply holds the allocation
while true; do
  sleep 300
done
