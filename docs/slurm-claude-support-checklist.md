# Slurm Claude Support Checklist

## Goal

Add `worker_mode=claude` support to the Slurm provider with behavior aligned to the existing AWS and local Claude runtimes, while preserving the Slurm-specific launch/bootstrap model.

## Implementation Steps

- [x] Step 1: Add Claude-capable Slurm launch fields in `router/providers/factory.py`
  - Add `worker_mode`
  - Add `approval_policy`
  - Add `fresh_thread_per_injection`
  - Add `claude_model`
  - Add `pricing_model`
  - Add optional `claude_env_profile`
  - Add optional `claude_cli_path`
  - Add optional `claude_permission_mode`
  - Keep `worker_mode=codex` as the default

- [x] Step 2: Extend Claude profile/model resolution for Slurm in `router/router.py`
  - Include `cluster.slurm.claude_env_profiles` in `_resolve_claude_profile_model()`
  - Preserve existing local and AWS behavior

- [x] Step 3: Add Claude env/runtime helpers to `router/providers/slurm.py`
  - Import shared helpers from `router/providers/claude_env.py`
  - Add runtime-mode helper
  - Add approval-policy helper
  - Add Claude permission-mode helper
  - Add resolved Claude env assembly from profile plus overrides

- [x] Step 4: Make Slurm provider launch runtime-aware in `router/providers/slurm.py`
  - Thread runtime launch params into `slurm/allocate_and_prepare.py`
  - Replace the Codex-only launch assumption with runtime selection
  - Keep Codex launch behavior backward compatible

- [x] Step 5: Refactor `slurm/allocate_and_prepare.py` into runtime-specific bootstrap paths
  - Split agent sync, tool installation, and SBATCH worker startup
  - Add `ensure_claude_ready(...)`
  - Add runtime-specific SBATCH worker commands for `codex_worker.py` vs `claude_worker.py`

- [x] Step 6: Stage Claude env securely for Slurm workers
  - Do not embed `ANTHROPIC_*` secrets directly in printed SBATCH scripts
  - Write per-launch env material on the login/shared filesystem without logging plaintext secrets
  - Source the staged env from the worker startup path

- [x] Step 7: Add Claude worker env parity
  - Set `CODESWARM_JOB_ID`
  - Set `CODESWARM_NODE_ID`
  - Set `CODESWARM_BASE_DIR`
  - Set `CODESWARM_ASK_FOR_APPROVAL`
  - Set `CODESWARM_CLAUDE_PERMISSION_MODE`
  - Optionally set `CODESWARM_CLAUDE_MODEL`
  - Optionally set `CODESWARM_CLAUDE_CLI_PATH`
  - Optionally set `CODESWARM_FRESH_THREAD_PER_INJECTION`
  - Inject resolved `ANTHROPIC_*`

- [x] Step 8: Keep follower and mailbox injection compatibility
  - Verify no Slurm follower changes are required
  - Confirm `claude_worker.py` output remains compatible with the existing router flow

- [x] Step 9: Add tests
  - Slurm launch fields expose Claude options
  - Slurm launch fields expose Claude env profile options
  - Router model/pricing resolution works for Slurm Claude profiles
  - Slurm provider passes correct runtime params to `allocate_and_prepare.py`
  - Secret-safe env staging is covered where practical

- [x] Step 10: Live validation
  - Direct Slurm Claude swarm launch
  - Direct worker injection
  - Browser-visible runtime activity
  - Clean termination

## Scope Notes

- First cut should target direct swarm/runtime support, not full orchestrated-project parity.
- Slurm repository preparation is separate work from runtime enablement.
- The main Slurm-specific risk is secret handling in the allocation/bootstrap path.
