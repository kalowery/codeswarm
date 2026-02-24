#!/bin/bash
#SBATCH --job-name=codex-cluster
#SBATCH --nodes=20
#SBATCH --ntasks-per-node=1
#SBATCH --time=04:00:00
#SBATCH --output=codex_cluster_%j.out
#SBATCH --error=codex_cluster_%j.err

# Create shared run directory
RUN_DIR=/shared/codex_cluster/${SLURM_JOB_ID}
mkdir -p ${RUN_DIR}

# Export run dir so agents know where to write
export CODEX_CLUSTER_RUN_DIR=${RUN_DIR}

# Launch one wrapper per node
srun python3 agent_wrapper.py
