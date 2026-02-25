# codeswarm — Current State & Recovery Summary

**Timestamp:** 2026-02-25 00:28 UTC  
**Status:** Transport layer stabilized; router now filters by Slurm job-name `codeswarm`.

---

## ✅ What Is Working

### 1. Slurm Job Launch
- Jobs are submitted with:
  ```bash
  #SBATCH --job-name=codeswarm
  ```
- Worker deployed to HPC before submission.
- One persistent Codex app-server per node.

### 2. Codex Integration
- Using: `codex app-server --listen stdio://`
- Proper LSP-style handshake implemented:
  - `initialize`
  - `initialized`
- Correct protocol methods:
  - `thread/start`
  - `turn/start`
- Structured JSON-RPC streaming working.

### 3. Worker
- Writes newline-terminated JSONL to:
  ```
  mailbox/outbox/<JOB_ID>_<NODE_ID>.jsonl
  ```
- Flushes after every write.
- Verified streaming deltas + final assistant message + token usage.

### 4. Router (Transport Layer)
- Uses persistent SSH streaming:
  ```
  ssh hpcfund tail -n 0 -F <explicit_active_job_files>
  ```
- Uses `select()` to multiplex stdout/stderr.
- Ignores tail header lines (`==> file <==`).
- Filters Slurm jobs using:
  ```bash
  squeue -h -n codeswarm -o %A
  ```
- No longer tails unrelated cluster jobs.

### 5. Proven Working Components
- SSH streaming works.
- JSON parsing works.
- Tail streaming works.
- select()-based non-blocking loop works.
- Active job filtering works.

---

## ⚠️ Known Behavior

- If no codeswarm jobs are running:
  ```
  No active Slurm jobs found.
  ```
  This is expected.

- `tail -n 0 -F` starts at EOF.
  → Messages written before router attaches are not replayed.
  (Switch to `-n +1` if historical replay is desired.)

---

## 📌 Remaining Validation Step

We still need one clean semantic validation pass:

1. Start router.
2. Launch a fresh `codeswarm` job.
3. Inject a user message via inbox.
4. Confirm router emits:
   - `turn_started`
   - `assistant_delta`
   - `assistant`
   - `usage`
   - `turn_complete`

Transport is confirmed stable; this is purely semantic verification.

---

## 🏗 Architecture Summary

OpenClaw (external)
        ↓
Router (semantic translator + SSH stream)
        ↓
Shared FS (mailbox)
        ↓
Worker (JSON-RPC relay)
        ↓
Codex app-server (structured protocol)

No PTY scraping.
No ANSI stripping.
No shell hacks.
No globbing races.

---

## 🚀 Next Recommended Improvements

1. Optional: switch to `tail -n +1 -F` for historical replay.
2. Add dynamic active-job refresh loop (detect new jobs after router starts).
3. Support multi-node job detection (`_01`, `_02`, etc.).
4. Remove remaining debug prints.
5. Integrate OpenClaw channel emission.

---

## ✅ Recovery Instructions (If Session Is Lost)

1. Open this file:
   ```
   codeswarm/STATE_SUMMARY_2026-02-25.md
   ```
2. Confirm router filters by job-name `codeswarm`.
3. Confirm worker uses `codex app-server` with LSP handshake.
4. Restart router and launch a fresh job for validation.

---

System is stable at transport layer.
Next phase is semantic validation and orchestration refinement.
