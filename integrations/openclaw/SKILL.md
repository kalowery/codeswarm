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

2. Use exec:

   codeswarm launch --nodes <N> --prompt "OpenClaw-managed swarm session." --config <codeswarm_config_path>

3. Parse output.
4. Extract swarm_id.
5. Store in session memory as:
   codeswarm_swarm_id
6. Confirm:
   ✅ Swarm launched: <swarm_id>

---

# Injecting Into A Swarm

If the user provides a prompt and a swarm exists:

1. Ensure codeswarm_swarm_id exists.
   - If not, ask user to launch first.

2. Use exec:

   codeswarm inject <codeswarm_swarm_id> --prompt "<user_prompt>" --config <codeswarm_config_path>

3. Allow streaming output to flow back to the user.

---

# Continuous Monitoring (Attach Mode)

If the user asks to:

- Monitor the swarm
- Continuously watch the swarm
- Stream swarm output
- Keep observing swarm activity

Then:

1. Ensure codeswarm_swarm_id exists.
2. Use exec in JSON streaming mode:

   codeswarm attach <codeswarm_swarm_id> --json --config <codeswarm_config_path>

3. Keep the exec session open.
4. Continuously read and process JSON event lines without waiting for further user prompts.
5. Do not expect human-formatted prefixes. Parse each line as structured JSON.

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

# Guardrails

- Never assume a default config path.
- Never launch without config set.
- Never inject without swarm_id.
- Always surface CLI errors clearly.
- Only use the exec tool to run codeswarm commands.
- For attach mode, do not terminate the session unless explicitly instructed.

---

# Memory Keys

Use session memory keys:

codeswarm_config_path
codeswarm_swarm_id

Do not use other names.

---

# Behavior Rules

- Be deterministic.
- Do not guess file paths.
- Ask user if required information is missing.
- Confirm state changes clearly.
- In attach mode, proactively process and organize streaming output without requiring new user prompts.
