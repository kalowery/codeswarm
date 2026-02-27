# Codeswarm

This skill integrates OpenClaw with the Codeswarm distributed swarm control plane.

It uses the `codeswarm` CLI via the `exec` tool.

---

# When To Use This Skill

Use this skill when the user wants to:

- Launch a distributed swarm
- Inject prompts into a swarm
- Monitor a running swarm continuously
- Check swarm status
- List active swarms
- Terminate a running swarm
- Manage distributed agent sessions on a cluster

---

# Configuration Management

Before launching a swarm, a Codeswarm config file must be set.

## Setting Config

If the user says:

Set Codeswarm config to <path>

Then:

1. Store <path> in session memory as:
   codeswarm_config_path
2. Confirm:
   ✅ Codeswarm config set to <path>

Do not call exec when setting config.

---

# Launching A Swarm

If the user says something equivalent to:

- Launch a swarm with <N> nodes
- Launch a <N> node swarm
- Start a distributed swarm with <N> nodes

Then:

1. Check if codeswarm_config_path exists in session memory.
   - If not set, ask the user to set it first.

2. Use exec in background mode:

   codeswarm launch --nodes <N> --prompt "OpenClaw-managed swarm session." --config <codeswarm_config_path>

   - Run with background monitoring enabled.
   - Do NOT block indefinitely waiting for completion.

3. Monitor the process using the process tool:
   - Poll periodically.
   - Capture stdout.
   - Detect success or failure.

4. When launch completes:
   - Parse output.
   - Extract swarm_id and job_id.
   - Add entry to codeswarm_swarms.
   - Set codeswarm_active_swarm_id.

5. Notify the user immediately upon completion.

6. If launch fails:
   - Surface the error clearly.
   - Do not require the user to manually re-check status.

The assistant should monitor background processes and report results when possible within the same execution window.

IMPORTANT LIMITATION:
If the execution window ends (timeout or control returns), the assistant cannot autonomously wake itself. In that case, it must clearly communicate:
- That the process is still running in the background.
- That it will check and report the latest status when the user next prompts it.

Do NOT imply guaranteed automatic future updates if no wake mechanism exists.

---

# Injecting Into A Swarm

If the user provides a prompt and a swarm exists:

1. Resolve target swarm using multi-swarm rules.

2. Use exec in background mode:

   codeswarm inject <swarm_id> --prompt "<user_prompt>" --config <codeswarm_config_path>

3. Monitor process using the process tool:
   - Stream output progressively.
   - Detect turn completion.
   - Detect failures.

4. Automatically notify the user when the injection completes.

5. Do NOT require the user to ask for results after injection.

The assistant must attempt to proactively report completion or failure while actively executing.

If execution control returns before completion, it must explicitly state that further updates require a user prompt.

---

# Continuous Monitoring (Attach Mode)

If the user asks to:

- Monitor the swarm
- Continuously watch the swarm
- Stream swarm output
- Keep observing swarm activity

Then:

1. Resolve target swarm using multi-swarm rules.

2. Use exec in background streaming mode:

   codeswarm attach <swarm_id> --config <codeswarm_config_path>

3. Keep the exec session open.
4. Continuously read and process streamed output.
5. Automatically surface lifecycle changes.
6. Do not terminate monitoring unless explicitly instructed.

The assistant must proactively stream updates without requiring additional prompts.

---

# Streaming Interpretation Rules

When streaming output from attach:

- Treat lines prefixed with:
  [swarm <swarm_id> | node <node_id>]
  as the start of a new logical message block.

- Group all subsequent streamed text under that swarm_id and node_id until the next prefix appears.

- Organize responses clearly in the conversation as:

  Swarm <swarm_id>
    Node <node_id>:
      <assistant output>

- Do not merge outputs from different nodes.
- Preserve ordering of streamed content.

---

# Swarm Status

If the user asks for swarm status:

1. Ensure swarm exists.
2. Use:

   codeswarm status <codeswarm_swarm_id> --config <codeswarm_config_path>

3. Display result clearly.

---

# List Swarms

If the user asks to list swarms:

1. Ensure codeswarm_config_path exists.
2. Use:

   codeswarm list --config <codeswarm_config_path>

3. Display all active swarms clearly.
4. Do not overwrite codeswarm_swarm_id unless user explicitly selects a swarm.

---

# Terminate Swarm

If the user asks to terminate a swarm:

1. Determine target swarm_id:
   - Use codeswarm_swarm_id if none specified.
   - If multiple swarms exist and none specified, ask user.

2. Use:

   codeswarm terminate <swarm_id> --config <codeswarm_config_path>

3. On success:
   ✅ Swarm terminated: <swarm_id>

4. Remove codeswarm_swarm_id from session memory if it matches the terminated swarm.

5. Surface CLI errors clearly.

---

---

# Guardrails

- Never assume a default config path.
- Never launch without config set.
- Never inject without swarm_id.
- Always surface CLI errors clearly.
- Only use the exec tool to run codeswarm commands.
- For attach mode, do not terminate the session unless explicitly instructed.

---

# Memory Keys

Use structured session memory keys:

codeswarm_config_path
codeswarm_active_swarm_id
codeswarm_swarms

Where:

codeswarm_swarms is a dictionary:

{
  "<swarm_id>": {
    "job_id": "<job_id>",
    "node_count": <number>,
    "status": "running|terminated|unknown"
  }
}

Rules:

- Always update codeswarm_swarms after launch, status, or terminate.
- Always set codeswarm_active_swarm_id after launch or attach.
- Never silently overwrite an existing swarm entry.

---

# Multi-Swarm Handling Rules

## Launch

- Add new swarm to codeswarm_swarms.
- Set codeswarm_active_swarm_id to the new swarm.

## List

- Do NOT change active swarm.
- Display all known swarms clearly.

## Inject

- If swarm_id explicitly provided → use it.
- Else if codeswarm_active_swarm_id exists → use it.
- Else if multiple swarms exist → ask user to choose.
- Never guess when multiple swarms exist.

## Terminate

- If swarm_id explicitly provided → use it.
- Else use codeswarm_active_swarm_id.
- Remove terminated swarm from codeswarm_swarms.
- If it was active:
  - If other swarms remain → set one as active.
  - Else clear codeswarm_active_swarm_id.

## Attach

- Explicitly bind to provided swarm_id.
- Set codeswarm_active_swarm_id to that swarm.

---

# Behavior Rules

- Be deterministic.
- Do not guess file paths.
- Ask user if required information is missing.
- Confirm state changes clearly.
- Never assume a single-swarm model.
- In attach mode, proactively process and organize streaming output without requiring new user prompts.
