#!/usr/bin/env node

import { Command } from "commander";
import { TcpTransport } from "./router/transport/TcpTransport.js";
import { spawn, spawnSync } from "child_process";
import { RouterClient } from "./router/RouterClient.js";
import { EventFormatter } from "./formatter/EventFormatter.js";
import fs from "fs/promises";
import { existsSync } from "fs";
import path from "path";
import process from "process";
import net from "net";
import { fileURLToPath } from "url";
import { runDoctor } from "./commands/doctor.js";

const program = new Command();

type ProviderParamValue = string | number | boolean;

type AgentsSkillFile = {
  path: string;
  content: string;
};

type AgentsBundlePayload = {
  mode: "file" | "directory";
  agents_md_content: string;
  skills_files: AgentsSkillFile[];
};

type LaunchCommandOptions = {
  provider?: string;
  providerParams?: Record<string, ProviderParamValue>;
  agentsMdContent?: string;
  agentsBundle?: AgentsBundlePayload;
};

type LaunchProvider = {
  id: string;
  label?: string;
  backend?: string;
  defaults?: Record<string, unknown>;
  launch_fields?: Array<{ key?: string }>;
  launch_panels?: Array<{ fields?: Array<{ key?: string }> }>;
};

const ROUTER_PID_FILENAME = "router.pid";

function formatCommandForLog(command: unknown): string {
  if (Array.isArray(command)) {
    return command.map((part) => String(part)).join(" ");
  }
  if (typeof command === "string") {
    return command;
  }
  if (command == null) {
    return "";
  }
  try {
    return JSON.stringify(command);
  } catch {
    return String(command);
  }
}

function formatDurationForLog(duration: any): string {
  const secs = Number(duration?.secs);
  const nanos = Number(duration?.nanos);
  if (!Number.isFinite(secs) && !Number.isFinite(nanos)) {
    return "";
  }
  const totalMs = (Number.isFinite(secs) ? secs : 0) * 1000 +
    (Number.isFinite(nanos) ? nanos / 1_000_000 : 0);
  return `${totalMs.toFixed(totalMs >= 1000 ? 0 : 1)}ms`;
}

function formatMultilinePreview(label: string, value: unknown): string | null {
  if (typeof value !== "string") {
    return null;
  }
  const trimmed = value.trim();
  if (!trimmed) {
    return null;
  }
  return `${label}:\n${trimmed}`;
}

class SwarmInfoLogger {
  private activeAssistantNodes = new Set<number>();

  constructor(private swarmId: string) {}

  handle(event: any) {
    if (event?.data?.swarm_id !== this.swarmId) {
      return;
    }

    const data = event.data;
    const node = Number.isFinite(Number(data?.node_id))
      ? Number(data.node_id)
      : undefined;
    const nodeLabel = Number.isFinite(node) ? `node ${node}` : "node ?";

    if (event.event === "turn_started") {
      console.log(`\n[INFO] ${nodeLabel} turn started`);
      return;
    }

    if (event.event === "assistant_delta") {
      if (!Number.isFinite(node)) return;
      const resolvedNode = Number(node);
      if (!this.activeAssistantNodes.has(resolvedNode)) {
        this.activeAssistantNodes.add(resolvedNode);
        process.stdout.write(`\n[${nodeLabel}] assistant\n`);
      }
      process.stdout.write(String(data?.content ?? ""));
      return;
    }

    if (event.event === "assistant") {
      if (!Number.isFinite(node)) return;
      const resolvedNode = Number(node);
      if (!this.activeAssistantNodes.has(resolvedNode) && typeof data?.content === "string") {
        process.stdout.write(`\n[${nodeLabel}] assistant\n${data.content}`);
      }
      return;
    }

    if (event.event === "turn_complete") {
      if (Number.isFinite(node)) {
        const resolvedNode = Number(node);
        if (this.activeAssistantNodes.has(resolvedNode)) {
          process.stdout.write("\n");
          this.activeAssistantNodes.delete(resolvedNode);
        }
      }
      console.log(`[INFO] ${nodeLabel} turn complete`);
      return;
    }

    if (event.event === "task_started") {
      console.log(`[INFO] ${nodeLabel} task started`);
      return;
    }

    if (event.event === "task_complete") {
      console.log(`[INFO] ${nodeLabel} task complete`);
      return;
    }

    if (event.event === "thread_status") {
      const status = String(data?.status?.type ?? "");
      if (status) {
        console.log(`[INFO] ${nodeLabel} status=${status}`);
      }
      return;
    }

    if (event.event === "command_started") {
      const commandText = formatCommandForLog(data?.command);
      const cwdSuffix =
        typeof data?.cwd === "string" && data.cwd.trim()
          ? ` (cwd: ${data.cwd})`
          : "";
      console.log(`[INFO] ${nodeLabel} command started: ${commandText}${cwdSuffix}`);
      return;
    }

    if (event.event === "command_completed") {
      const commandText = formatCommandForLog(data?.command);
      const exitCode =
        typeof data?.exit_code === "number" ? data.exit_code : "unknown";
      const durationText = formatDurationForLog(data?.duration);
      const durationSuffix = durationText ? ` in ${durationText}` : "";
      console.log(
        `[INFO] ${nodeLabel} command completed (exit ${exitCode})${durationSuffix}: ${commandText}`
      );
      const stderrPreview =
        exitCode !== 0 ? formatMultilinePreview("stderr", data?.stderr) : null;
      if (stderrPreview) {
        console.log(stderrPreview);
      }
      return;
    }

    if (event.event === "exec_approval_required") {
      const reason =
        typeof data?.reason === "string" && data.reason.trim()
          ? `: ${data.reason.trim()}`
          : "";
      console.log(`[INFO] ${nodeLabel} approval required${reason}`);
      return;
    }

    if (event.event === "exec_approval_resolved") {
      const approved =
        typeof data?.approved === "boolean"
          ? data.approved
            ? "approved"
            : "denied"
          : "resolved";
      console.log(`[INFO] ${nodeLabel} approval ${approved}`);
      return;
    }

    if (event.event === "usage") {
      if (typeof data?.total_tokens === "number") {
        console.log(`[INFO] ${nodeLabel} total tokens=${data.total_tokens}`);
      }
      return;
    }

    if (event.event === "agent_error") {
      const message = data?.message ? String(data.message) : "Unknown error";
      console.error(`[ERROR] ${nodeLabel} agent error: ${message}`);
    }
  }
}

function isCompatiblePython(command: string): boolean {
  try {
    const probe = spawnSync(
      command,
      ["-c", "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')"],
      { encoding: "utf-8" }
    );

    if (probe.status !== 0) return false;
    const version = String(probe.stdout || "").trim();
    const [majorRaw, minorRaw] = version.split(".");
    const major = Number(majorRaw);
    const minor = Number(minorRaw);
    return Number.isFinite(major) && Number.isFinite(minor) && (major > 3 || (major === 3 && minor >= 10));
  } catch {
    return false;
  }
}

function resolvePythonCommand(): string {
  const override = process.env.CODESWARM_PYTHON;
  if (override && isCompatiblePython(override)) {
    return override;
  }

  const candidates = ["python3.12", "python3.11", "python3.10", "python3"];
  for (const cmd of candidates) {
    if (isCompatiblePython(cmd)) {
      return cmd;
    }
  }

  throw new Error(
    "No compatible Python found (requires 3.10+). " +
    "Install Python 3.10+ or set CODESWARM_PYTHON to a compatible interpreter."
  );
}

function collectRepeatedOption(value: string, previous: string[] = []): string[] {
  previous.push(value);
  return previous;
}

function parseProviderParamValue(rawValue: string): ProviderParamValue {
  const trimmed = rawValue.trim();
  const lower = trimmed.toLowerCase();

  if (lower === "true") return true;
  if (lower === "false") return false;
  if (/^-?(?:\d+|\d*\.\d+)$/.test(trimmed)) {
    const parsed = Number(trimmed);
    if (Number.isFinite(parsed)) {
      return parsed;
    }
  }

  return rawValue;
}

function assertPrimitiveProviderParams(
  value: unknown,
  sourceLabel: string
): Record<string, ProviderParamValue> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${sourceLabel} must decode to a JSON object.`);
  }

  const out: Record<string, ProviderParamValue> = {};
  for (const [key, entry] of Object.entries(value)) {
    if (
      typeof entry !== "string" &&
      typeof entry !== "number" &&
      typeof entry !== "boolean"
    ) {
      throw new Error(
        `${sourceLabel} values must be strings, numbers, or booleans (invalid key: ${key}).`
      );
    }
    out[key] = entry;
  }
  return out;
}

function parseProviderParams(cmd: any): Record<string, ProviderParamValue> | undefined {
  const params: Record<string, ProviderParamValue> = {};

  if (typeof cmd.providerParamsJson === "string" && cmd.providerParamsJson.trim()) {
    const parsed = JSON.parse(cmd.providerParamsJson);
    Object.assign(
      params,
      assertPrimitiveProviderParams(parsed, "--provider-params-json")
    );
  }

  const repeated = Array.isArray(cmd.providerParam)
    ? (cmd.providerParam as string[])
    : [];
  for (const entry of repeated) {
    const separator = entry.indexOf("=");
    if (separator <= 0) {
      throw new Error(
        `Invalid --provider-param '${entry}'. Expected key=value.`
      );
    }

    const key = entry.slice(0, separator).trim();
    const rawValue = entry.slice(separator + 1);
    if (!key) {
      throw new Error(
        `Invalid --provider-param '${entry}'. Parameter key cannot be empty.`
      );
    }
    params[key] = parseProviderParamValue(rawValue);
  }

  return Object.keys(params).length > 0 ? params : undefined;
}

async function readAgentsSkillsDir(
  dirPath: string,
  prefix = ""
): Promise<AgentsSkillFile[]> {
  const entries = await fs.readdir(dirPath, { withFileTypes: true });
  entries.sort((a, b) => a.name.localeCompare(b.name));

  const out: AgentsSkillFile[] = [];
  for (const entry of entries) {
    const relPath = prefix ? `${prefix}/${entry.name}` : entry.name;
    const absolutePath = path.join(dirPath, entry.name);
    if (entry.isDirectory()) {
      out.push(...(await readAgentsSkillsDir(absolutePath, relPath)));
      continue;
    }
    if (!entry.isFile()) {
      continue;
    }
    out.push({
      path: relPath,
      content: await fs.readFile(absolutePath, "utf8"),
    });
  }
  return out;
}

async function resolveAgentsLaunchOptions(
  agentsPathInput?: string
): Promise<Partial<LaunchCommandOptions>> {
  if (!agentsPathInput || !agentsPathInput.trim()) {
    return {};
  }

  const resolvedPath = path.resolve(process.cwd(), agentsPathInput);
  const stats = await fs.stat(resolvedPath);

  if (stats.isFile()) {
    const content = await fs.readFile(resolvedPath, "utf8");
    return {
      agentsMdContent: content,
      agentsBundle: {
        mode: "file",
        agents_md_content: content,
        skills_files: [],
      },
    };
  }

  if (!stats.isDirectory()) {
    throw new Error(
      `Unsupported --agents path: ${agentsPathInput} (expected file or directory).`
    );
  }

  const agentsMdPath = path.join(resolvedPath, "AGENTS.md");
  let agentsMdContent: string;
  try {
    agentsMdContent = await fs.readFile(agentsMdPath, "utf8");
  } catch (error: any) {
    if (error?.code === "ENOENT") {
      throw new Error(
        `Persona directory ${agentsPathInput} must contain AGENTS.md at its root.`
      );
    }
    throw error;
  }

  const skillsDir = path.join(resolvedPath, "skills");
  let skillsFiles: AgentsSkillFile[] = [];
  try {
    const skillsStats = await fs.stat(skillsDir);
    if (skillsStats.isDirectory()) {
      skillsFiles = await readAgentsSkillsDir(skillsDir);
    }
  } catch (error: any) {
    if (error?.code !== "ENOENT") {
      throw error;
    }
  }

  return {
    agentsMdContent,
    agentsBundle: {
      mode: "directory",
      agents_md_content: agentsMdContent,
      skills_files: skillsFiles,
    },
  };
}

function extractProviderFieldKeys(provider: LaunchProvider): string[] {
  const keys = new Set<string>();

  for (const field of provider.launch_fields ?? []) {
    if (typeof field?.key === "string" && field.key.trim()) {
      keys.add(field.key);
    }
  }

  for (const panel of provider.launch_panels ?? []) {
    for (const field of panel.fields ?? []) {
      if (typeof field?.key === "string" && field.key.trim()) {
        keys.add(field.key);
      }
    }
  }

  return Array.from(keys.values()).sort();
}

function printProviders(providers: LaunchProvider[]) {
  if (providers.length === 0) {
    console.log("No launch providers available.");
    return;
  }

  console.log("\nLaunch Providers:");
  console.log("------------------------------------------------------------");
  for (const provider of providers) {
    const header = [
      provider.id,
      provider.backend ? `backend=${provider.backend}` : undefined,
      provider.label ? `label=${provider.label}` : undefined,
    ]
      .filter(Boolean)
      .join(" | ");
    console.log(header);

    const fieldKeys = extractProviderFieldKeys(provider);
    if (fieldKeys.length > 0) {
      console.log(`  params: ${fieldKeys.join(", ")}`);
    }
  }
  console.log("------------------------------------------------------------\n");
}

function getRepoRoot(): string {
  const cwd = process.cwd();
  const cwdLooksLikeRepo =
    existsSync(path.join(cwd, "router", "router.py")) &&
    existsSync(path.join(cwd, "web", "backend")) &&
    existsSync(path.join(cwd, "web", "frontend"));
  if (cwdLooksLikeRepo) {
    return cwd;
  }

  const __filename = fileURLToPath(import.meta.url);
  const __dirname = path.dirname(__filename);
  return path.resolve(__dirname, "../../");
}

function getRouterPidFilePath(): string {
  return path.join(getRepoRoot(), ROUTER_PID_FILENAME);
}

async function isRouterRunning(host: string, port: number): Promise<boolean> {
  return new Promise((resolve) => {
    const socket = new net.Socket();

    socket.setTimeout(300);

    socket
      .once("connect", () => {
        socket.destroy();
        resolve(true);
      })
      .once("error", () => {
        resolve(false);
      })
      .once("timeout", () => {
        socket.destroy();
        resolve(false);
      })
      .connect(port, host);
  });
}

function resolveRouterAddress(opts: any): { host: string; port: number } {
  let host = "127.0.0.1";
  let port = 8765;

  if (opts.router) {
    const [h, p] = String(opts.router).split(":");
    host = h || host;
    if (p) {
      const parsed = parseInt(p, 10);
      if (!isNaN(parsed)) {
        port = parsed;
      }
    }
  }

  return { host, port };
}

async function stopRouterProcess(opts: any): Promise<boolean> {
  const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));
  const waitForExit = async (pid: number, timeoutMs: number): Promise<boolean> => {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      try {
        process.kill(pid, 0);
      } catch (error: any) {
        if (error?.code === "ESRCH") {
          return true;
        }
      }
      await sleep(100);
    }
    try {
      process.kill(pid, 0);
      return false;
    } catch (error: any) {
      return error?.code === "ESRCH";
    }
  };
  const terminatePid = async (pid: number): Promise<boolean> => {
    try {
      process.kill(pid, "SIGTERM");
    } catch (error: any) {
      if (error?.code === "ESRCH") {
        return false;
      }
      throw error;
    }
    if (await waitForExit(pid, 2000)) {
      return true;
    }
    try {
      process.kill(pid, "SIGKILL");
    } catch (error: any) {
      if (error?.code === "ESRCH") {
        return true;
      }
      throw error;
    }
    return await waitForExit(pid, 1000);
  };

  const pidFile = getRouterPidFilePath();
  try {
    const raw = await fs.readFile(pidFile, "utf8");
    const pid = parseInt(raw.trim(), 10);
    if (Number.isFinite(pid) && pid > 0) {
      const stopped = await terminatePid(pid);
      if (stopped) {
        return true;
      }
    }
  } catch {}

  const configPath = opts.config ? path.resolve(process.cwd(), String(opts.config)) : null;
  const configBasename = configPath ? path.basename(configPath) : null;
  const probe = spawnSync("pgrep", ["-af", "router.router"], { encoding: "utf-8" });
  if (probe.status !== 0) {
    return false;
  }

  const pids = String(probe.stdout || "")
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean)
    .filter((line) => line.includes("--daemon"))
    .filter((line) => {
      if (!configPath) return true;
      return line.includes(configPath) || (configBasename ? line.includes(configBasename) : false);
    })
    .map((line) => parseInt(line.split(/\s+/, 1)[0] || "", 10))
    .filter((value) => Number.isFinite(value) && value > 0);

  let stopped = false;
  for (const pid of pids) {
    try {
      if (await terminatePid(pid)) {
        stopped = true;
      }
    } catch {}
  }
  return stopped;
}

async function requestSwarms(client: RouterClient): Promise<Record<string, any>> {
  return await new Promise<Record<string, any>>((resolve, reject) => {
    const requestId = client.listSwarms();
    client.onEvent((e) => {
      if (e?.data?.request_id !== requestId) return;
      if (e.event === "swarm_list") {
        resolve((e.data?.swarms as Record<string, any>) || {});
        return;
      }
      if (e.event === "command_rejected") {
        reject(new Error(e.data?.reason || "Command rejected"));
      }
    });
  });
}

async function terminateSwarmAndWait(
  client: RouterClient,
  swarmId: string
): Promise<void> {
  await new Promise<void>((resolve, reject) => {
    const requestId = client.terminate(swarmId);
    client.onEvent((e) => {
      if (e?.data?.request_id !== requestId) return;
      if (e.event === "swarm_terminated") {
        resolve();
        return;
      }
      if (e.event === "command_rejected") {
        reject(new Error(e.data?.reason || "Command rejected"));
      }
    });
  });
}

program
  .name("codeswarm")
  .description("Codeswarm CLI")
  .showHelpAfterError()
  .enablePositionalOptions()
  .helpOption("-h, --help", "Display help for command");

program
  .command("list")
  .description("List available swarms")
  .option("--config <path>", "Path to router config")
  .option("--router <address>", "Router address override (host:port)")
  .option("--debug", "Print raw JSON messages from router", false)
  .action(async (cmd: any) => {
    const opts = cmd;

    const transport = await createTransport(opts);
    const client = new RouterClient(transport);
    const formatter = new EventFormatter();

    let requestId: string | null = null;

    client.onEvent((e) => {
      if (!requestId || e?.data?.request_id !== requestId) return;

      switch (e.event) {
        case "swarm_list":
          formatter.handle(e);
          process.exit(0);
          break;
        case "command_rejected":
          console.error("Error:", e.data?.reason || "Command rejected");
          process.exit(1);
          break;
        default:
          console.error("Unexpected response:", e.event);
          process.exit(1);
      }
    });

    requestId = client.listSwarms();
  });

program
  .command("status <swarmId>")
  .description("Get swarm status")
  .option("--config <path>", "Path to router config")
  .option("--router <address>", "Router address override (host:port)")
  .option("--debug", "Print raw JSON messages from router", false)
  .action(async (swarmId: string, cmd: any) => {
    const opts = cmd;

    const transport = await createTransport(opts);
    const client = new RouterClient(transport);
    const formatter = new EventFormatter();

    let requestId: string | null = null;

    client.onEvent((e) => {
      if (!requestId || e?.data?.request_id !== requestId) return;

      switch (e.event) {
        case "swarm_status":
          formatter.handle(e);
          process.exit(0);
          break;
        case "command_rejected":
          console.error("Error:", e.data?.reason || "Command rejected");
          process.exit(1);
          break;
        default:
          console.error("Unexpected response:", e.event);
          process.exit(1);
      }
    });

    requestId = client.status(swarmId);
  });

program
  .command("launch")
  .description("Launch new swarm")
  .requiredOption("--nodes <number>", "Number of nodes")
  .requiredOption("--prompt <text>", "System prompt")
  .option("--provider <providerId>", "Launch provider preset id")
  .option(
    "--provider-param <key=value>",
    "Provider parameter override; repeat for multiple values",
    collectRepeatedOption,
    []
  )
  .option(
    "--provider-params-json <json>",
    "Provider parameter overrides as a JSON object"
  )
  .option(
    "--agents <path>",
    "Path to AGENTS.md file or persona directory containing AGENTS.md and optional skills/"
  )
  .option("--detach", "Exit after swarm launch instead of following INFO activity logs", false)
  .option("--config <path>", "Path to router config")
  .option("--router <address>", "Router address override (host:port)")
  .option("--debug", "Print raw JSON messages from router", false)
  .action(async (cmd: any) => {
    const opts = cmd;
    const parsedNodeCount = parseInt(String(cmd.nodes), 10);
    if (Number.isNaN(parsedNodeCount) || parsedNodeCount < 1) {
      throw new Error("--nodes must be a positive integer.");
    }

    const transport = await createTransport(opts);
    const client = new RouterClient(transport);
    const providerParams = parseProviderParams(cmd);
    const agentsOptions = await resolveAgentsLaunchOptions(cmd.agents);
    const launchOptions: LaunchCommandOptions = {
      ...agentsOptions,
    };
    if (typeof cmd.provider === "string" && cmd.provider.trim()) {
      launchOptions.provider = cmd.provider;
    }
    if (providerParams) {
      launchOptions.providerParams = providerParams;
    }

    let requestId: string | null = null;
    let launchedSwarmId: string | null = null;
    let infoLogger: SwarmInfoLogger | null = null;

    client.onEvent((e) => {
      if (
        launchedSwarmId &&
        e?.data?.swarm_id === launchedSwarmId &&
        infoLogger
      ) {
        infoLogger.handle(e);
        return;
      }

      if (!requestId || e?.data?.request_id !== requestId) return;

      switch (e.event) {
        case "swarm_launch_progress": {
          const stage = e.data?.stage ? String(e.data.stage) : "progress";
          const message = e.data?.message ? String(e.data.message) : "";
          if (message) {
            console.log(`[launch:${stage}] ${message}`);
          } else {
            console.log(`[launch:${stage}]`);
          }
          break;
        }
        case "swarm_launched":
          console.log("Swarm launched successfully:\n");
          Object.entries(e.data).forEach(([k, v]) => {
            console.log(`${k}: ${v}`);
          });
          if (cmd.detach) {
            process.exit(0);
          }
          launchedSwarmId =
            typeof e.data?.swarm_id === "string" ? e.data.swarm_id : null;
          if (!launchedSwarmId) {
            console.error("Launch succeeded but swarm_id was missing from the response.");
            process.exit(1);
          }
          infoLogger = new SwarmInfoLogger(launchedSwarmId);
          console.log(`\n[INFO] Following activity logs for swarm ${launchedSwarmId}`);
          console.log("[INFO] Press Ctrl+C to detach.\n");
          break;
        case "command_rejected":
          console.error("Launch failed:", e.data?.reason || "Command rejected");
          process.exit(1);
          break;
        default:
          console.error("Unexpected response:", e.event);
          process.exit(1);
      }
    });

    requestId = client.launch(parsedNodeCount, cmd.prompt, launchOptions);

    if (!cmd.detach) {
      process.on("SIGINT", () => {
        console.log("\nDetached.");
        process.exit(0);
      });
      await new Promise<void>(() => {});
    }
  });

program
  .command("providers")
  .description("List launch providers and accepted provider parameters")
  .option("--config <path>", "Path to router config")
  .option("--router <address>", "Router address override (host:port)")
  .option("--debug", "Print raw JSON messages from router", false)
  .option("--json", "Print providers as JSON", false)
  .action(async (cmd: any) => {
    const opts = cmd;
    const transport = await createTransport(opts);
    const client = new RouterClient(transport);

    let requestId: string | null = null;

    client.onEvent((e) => {
      if (!requestId || e?.data?.request_id !== requestId) return;

      switch (e.event) {
        case "providers_list": {
          const providers = Array.isArray(e.data?.providers)
            ? (e.data.providers as LaunchProvider[])
            : [];
          if (cmd.json) {
            console.log(JSON.stringify(providers, null, 2));
          } else {
            printProviders(providers);
          }
          process.exit(0);
          break;
        }
        case "command_rejected":
          console.error(
            "Provider lookup failed:",
            e.data?.reason || "Command rejected"
          );
          process.exit(1);
          break;
        default:
          console.error("Unexpected response:", e.event);
          process.exit(1);
      }
    });

    requestId = client.providers();
  });

program
  .command("inject <swarmId>")
  .description("Inject prompt into swarm")
  .requiredOption("--prompt <text>", "Prompt text")
  .option("--nodes <nodes>", "Node index or 'all'", "all")
  .option("--config <path>", "Path to router config")
  .option("--router <address>", "Router address override (host:port)")
  .option("--debug", "Print raw JSON messages from router", false)
  .action(async (swarmId: string, cmd: any) => {
    const opts = cmd;

    const transport = await createTransport(opts);
    const client = new RouterClient(transport);
    const formatter = new EventFormatter();

    // ---- Step 1: Bootstrap status to determine node count ----
    const statusRequestId = client.status(swarmId);

    let nodeCount: number | null = null;

    await new Promise<void>((resolve) => {
      const statusListener = (e: any) => {
        if (e?.data?.request_id !== statusRequestId) return;
        if (e.event === "swarm_status") {
          nodeCount = e?.data?.node_count ?? null;
          resolve();
        } else if (e.event === "command_rejected") {
          console.error("Error:", e.data?.reason || "Command rejected");
          process.exit(1);
        }
      };
      client.onEvent(statusListener);
    });

    if (nodeCount === null) {
      console.error("Unable to determine node count for swarm.");
      process.exit(1);
    }

    // ---- Step 2: Resolve target nodes ----
    let targetNodes: number[] = [];

    if (cmd.nodes === "all") {
      targetNodes = Array.from({ length: nodeCount }, (_, i) => i);
    } else {
      const idx = parseInt(cmd.nodes);
      if (isNaN(idx) || idx < 0 || idx >= nodeCount) {
        console.error(`Invalid node index: ${cmd.nodes}`);
        process.exit(1);
      }
      targetNodes = [idx];
    }

    // ---- Step 3: Inject and track request_ids + injection_ids ----
    const injectionRequestIds = new Set<string>();

    // Track lifecycle per node
    const nodeLifecycle = new Map<
      number,
      { turnComplete: boolean; delivered: boolean; assistantSeen: boolean }
    >();

    const maybeExit = () => {
      if (targetNodes.length === 0) return;

      for (const node of targetNodes) {
        const state = nodeLifecycle.get(node);
        if (
          !state ||
          !state.delivered ||
          !state.assistantSeen ||
          !state.turnComplete
        ) {
          return;
        }
      }

      // All targeted nodes have delivered, produced assistant output, and completed turn
      setImmediate(() => process.exit(0));
    };

    const injectListener = (e: any) => {
      const reqId = e?.data?.request_id;

      // ---- Handle inject command-level events ----
      if (reqId && injectionRequestIds.has(reqId)) {
        if (e.event === "inject_ack") {
          const nodeId = e.data.node_id;
          nodeLifecycle.set(nodeId, {
            turnComplete: false,
            delivered: false,
            assistantSeen: false,
          });
          formatter.handle(e);
          return;
        }
        if (e.event === "inject_failed") {
          console.error("Inject failed:", e.data?.error || "Unknown error");
          process.exit(1);
        }
        if (e.event === "inject_delivered") {
          const nodeId = e.data.node_id;
          const state = nodeLifecycle.get(nodeId);
          if (state) {
            state.delivered = true;
          }
          formatter.handle(e);
          maybeExit();
          return;
        }
      }

      // ---- Handle streaming assistant events by swarm + node ----
      if (
        e?.data?.swarm_id === swarmId &&
        typeof e?.data?.node_id === "number" &&
        targetNodes.includes(e.data.node_id)
      ) {
        formatter.handle(e);

        const state = nodeLifecycle.get(e.data.node_id);

        if (state) {
          if (e.event === "assistant") {
            state.assistantSeen = true;
          }

          if (e.event === "turn_complete") {
            state.turnComplete = true;
          }
        }

        maybeExit();
      }
    };

    client.onEvent(injectListener);

    for (const nodeIdx of targetNodes) {
      const reqId = client.inject(swarmId, nodeIdx, cmd.prompt);
      injectionRequestIds.add(reqId);
    }
  });

async function createTransport(opts: any, transportOpts?: { autoStartRouter?: boolean }) {
  const pythonCmd = resolvePythonCommand();
  let routerStartedByWeb = false;
  const { host, port } = resolveRouterAddress(opts);
  const autoStartRouter = transportOpts?.autoStartRouter !== false;

  if (!opts.router) {
    const running = await isRouterRunning(host, port);

    if (running) {
      if (!opts.json) {
        console.error("[codeswarm] Using existing router.");
      }
    } else {
      if (!autoStartRouter) {
        throw new Error("Router is not running.");
      }
      if (!opts.config) {
        throw new Error(
          "No router running and no --config provided. Cannot start embedded router."
        );
      }

      if (!opts.json) {
        console.error("[codeswarm] Starting embedded router...");
      }

      const repoRoot = getRepoRoot();

      const routerProcess = spawn(
        pythonCmd,
        ["-u", "-m", "router.router", "--config", opts.config, "--daemon"],
        {
          detached: true,
          stdio: "ignore",
          cwd: repoRoot
        }
      );
      routerProcess.unref();
    }
  }

  return new TcpTransport(host, port, opts.debug === true);
}


program
  .command("terminate <swarmId>")
  .description("Terminate a running swarm")
  .option("--config <path>", "Path to router config")
  .option("--router <address>", "Router address override (host:port)")
  .option("--debug", "Print raw JSON messages from router", false)
  .action(async (swarmId: string, cmd: any) => {
    const opts = cmd;

    const transport = await createTransport(opts);
    const client = new RouterClient(transport);

    let requestId: string | null = null;

    client.onEvent((e) => {
      if (!requestId || e?.data?.request_id !== requestId) return;

      switch (e.event) {
        case "swarm_status":
        case "swarm_terminate_progress":
        case "queue_updated":
          // Intermediate events during asynchronous termination.
          return;
        case "swarm_terminated":
          console.log(`Swarm ${swarmId} terminated.`);
          process.exit(0);
          break;
        case "command_rejected":
          console.error("Terminate failed:", e.data?.reason || "Command rejected");
          process.exit(1);
          break;
        default:
          console.error("Unexpected response:", e.event);
          process.exit(1);
      }
    });

    requestId = client.terminate(swarmId);
  });

program
  .command("stop-all")
  .description("Terminate all known swarms and stop the local router")
  .option("--config <path>", "Path to router config")
  .option("--router <address>", "Router address override (host:port)")
  .option("--keep-router", "Terminate all swarms but leave the router running", false)
  .option("--debug", "Print raw JSON messages from router", false)
  .action(async (cmd: any) => {
    const opts = cmd;
    const { host, port } = resolveRouterAddress(opts);
    const running = await isRouterRunning(host, port);

    if (!running) {
      const stopped = cmd.keepRouter ? false : await stopRouterProcess(opts);
      if (stopped) {
        console.log("Router stopped.");
      } else {
        console.log("No running router found.");
      }
      return;
    }

    const transport = await createTransport(opts, { autoStartRouter: false });
    const client = new RouterClient(transport);
    const swarms = await requestSwarms(client);
    const entries = Object.entries(swarms);

    if (entries.length === 0) {
      console.log("No swarms to stop.");
    } else {
      for (const [swarmId, info] of entries) {
        const label = typeof (info as any)?.job_id === "string"
          ? `${swarmId} (${(info as any).job_id})`
          : swarmId;
        console.log(`Stopping ${label}...`);
        await terminateSwarmAndWait(client, swarmId);
      }
      console.log(`Stopped ${entries.length} swarm(s).`);
    }

    transport.close();

    if (!cmd.keepRouter) {
      const stopped = await stopRouterProcess(opts);
      if (stopped) {
        console.log("Router stopped.");
      } else {
        console.log("Router stop requested, but no router pid was found.");
      }
    }
  });

program
  .command("attach <swarmId>")
  .description("Attach to a running swarm and stream events continuously")
  .option("--config <path>", "Path to router config")
  .option("--router <address>", "Router address override (host:port)")
  .option("--debug", "Print raw JSON messages from router", false)
  .option("--json", "Emit raw router events as JSON lines", false)
  .action(async (swarmId: string, cmd: any) => {
    const opts = cmd;
    const jsonMode = opts.json === true;

    const transport = await createTransport(opts);
    const client = new RouterClient(transport);
    const infoLogger = new SwarmInfoLogger(swarmId);

    // Validate swarm_id first
    const statusRequestId = client.status(swarmId);
    let validated = false;

    client.onEvent((e: any) => {
      // Validation phase
      if (!validated && e?.data?.request_id === statusRequestId) {
        if (e.event === "command_rejected") {
          if (jsonMode) {
            console.log(JSON.stringify(e));
          } else {
            console.error("Invalid swarm_id.");
          }
          process.exit(1);
        }

        if (e.event === "swarm_status") {
          validated = true;
          if (!jsonMode) {
            console.log(`🔗 Attached to swarm ${swarmId}`);
            console.log("Press Ctrl+C to detach.\n");
          }
        }
        return;
      }

      if (!validated) return;

      // JSON mode: emit structured events filtered by swarm_id
      if (jsonMode) {
        if (e?.data?.swarm_id === swarmId) {
          console.log(JSON.stringify(e));
        }
        return;
      }

      // Human streaming mode
      if (
        e?.data?.swarm_id === swarmId &&
        typeof e?.data?.node_id === "number"
      ) {
        infoLogger.handle(e);
      }
    });

    process.on("SIGINT", () => {
      if (!jsonMode) {
        console.log("\nDetached.");
      }
      process.exit(0);
    });

    await new Promise<void>(() => {});
  });

// --- Web Stack Supervisor ---

async function runWebStack(opts: any) {
  const __filename = fileURLToPath(import.meta.url);
  const __dirname = path.dirname(__filename);
  const repoRoot = path.resolve(__dirname, "../../");

  // routerPath no longer used (router launched as module)
  const routerModule = "router.router";
  const backendPath = path.join(repoRoot, "web", "backend");
  const frontendPath = path.join(repoRoot, "web", "frontend");

  const configPath = opts.config
    ? path.resolve(process.cwd(), opts.config)
    : path.join(repoRoot, "configs", "hpcfund.json");
  const pythonCmd = resolvePythonCommand();

  console.log("[web] Starting Codeswarm web stack...\n");

  const children: any[] = [];
  let routerStartedByWeb = false;

  const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));
  const frontendUrl = "http://localhost:3000";
  const backendUrl = "http://localhost:4000/swarms";

  async function isTcpPortListening(host: string, port: number, timeoutMs = 700): Promise<boolean> {
    return await new Promise<boolean>((resolve) => {
      const socket = new net.Socket();
      let settled = false;
      const finish = (result: boolean) => {
        if (settled) return;
        settled = true;
        try {
          socket.destroy();
        } catch {}
        resolve(result);
      };

      socket.setTimeout(timeoutMs);
      socket.once("connect", () => finish(true));
      socket.once("timeout", () => finish(false));
      socket.once("error", () => finish(false));
      socket.connect(port, host);
    });
  }

  async function probeRouterHealth(
    host: string,
    port: number,
    timeoutMs = 1500
  ): Promise<boolean> {
    return await new Promise<boolean>((resolve) => {
      const socket = new net.Socket();
      const requestId = `health-${Date.now()}-${Math.random().toString(16).slice(2)}`;
      let settled = false;
      let buffer = "";

      const finish = (result: boolean) => {
        if (settled) return;
        settled = true;
        try {
          socket.destroy();
        } catch {}
        resolve(result);
      };

      socket.setTimeout(timeoutMs);
      socket.once("connect", () => {
        const envelope = {
          protocol: "codeswarm.router.v1",
          type: "command",
          command: "providers_list",
          request_id: requestId,
          payload: {},
        };
        socket.write(JSON.stringify(envelope) + "\n");
      });
      socket.on("data", (chunk) => {
        buffer += chunk.toString();
        const lines = buffer.split("\n");
        buffer = lines.pop() || "";
        for (const line of lines) {
          if (!line.trim()) continue;
          try {
            const msg = JSON.parse(line);
            if (
              msg?.type === "event" &&
              msg?.event === "providers_list" &&
              msg?.data?.request_id === requestId
            ) {
              finish(true);
              return;
            }
          } catch {}
        }
      });
      socket.once("timeout", () => finish(false));
      socket.once("error", () => finish(false));
      socket.once("close", () => finish(false));
      socket.connect(port, host);
    });
  }

  async function waitForRouterHealthy(
    host: string,
    port: number,
    timeoutMs: number
  ): Promise<boolean> {
    const start = Date.now();
    while (Date.now() - start < timeoutMs) {
      if (await probeRouterHealth(host, port, 1200)) {
        return true;
      }
      await sleep(300);
    }
    return false;
  }

  async function checkHttpReady(url: string, timeoutMs: number): Promise<boolean> {
    try {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), timeoutMs);
      const response = await fetch(url, { signal: controller.signal });
      clearTimeout(timer);
      return response.status < 500;
    } catch {
      return false;
    }
  }

  async function waitForWebReady(timeoutMs: number): Promise<boolean> {
    const start = Date.now();

    while (Date.now() - start < timeoutMs) {
      const [frontendReady, backendReady] = await Promise.all([
        checkHttpReady(frontendUrl, 1500),
        checkHttpReady(backendUrl, 1500),
      ]);
      if (frontendReady && backendReady) {
        return true;
      }
      await sleep(500);
    }

    return false;
  }

  function spawnWithPrefix(name: string, cmd: string, args: string[], cwd?: string) {
    const child = spawn(cmd, args, {
      cwd,
      stdio: ["ignore", "pipe", "pipe"],
    });

    child.stdout.on("data", (data) => {
      process.stdout.write(`[${name}] ${data}`);
    });

    child.stderr.on("data", (data) => {
      process.stderr.write(`[${name}] ${data}`);
    });

    child.on("exit", (code) => {
      console.log(`[${name}] exited with code ${code}`);
    });

    children.push(child);
  }

  // Router (daemon + debug mode)
  // Reuse existing router instead of crashing on "Address already in use".
  const routerHost = "127.0.0.1";
  const routerPort = 8765;
  const routerAlreadyListening = await isTcpPortListening(routerHost, routerPort);
  if (routerAlreadyListening) {
    const healthy = await probeRouterHealth(routerHost, routerPort, 1500);
    if (healthy) {
      console.log(
        `[web] Router already listening on ${routerHost}:${routerPort}; reusing existing process.`
      );
    } else {
      console.warn(
        `[web] Existing router on ${routerHost}:${routerPort} failed health probe; attempting restart.`
      );
      await stopRouterProcess({ config: configPath });
      const restartDeadline = Date.now() + 5000;
      while (Date.now() < restartDeadline) {
        if (!(await isTcpPortListening(routerHost, routerPort, 300))) {
          break;
        }
        await sleep(250);
      }
      if (await isTcpPortListening(routerHost, routerPort, 300)) {
        throw new Error(
          `Router port ${routerHost}:${routerPort} is occupied by an unhealthy process.`
        );
      }
      spawnWithPrefix(
        "router",
        pythonCmd,
        ["-u", "-m", "router.router", "--config", configPath, "--daemon", "--debug"],
        repoRoot
      );
      routerStartedByWeb = true;
    }
  } else {
    spawnWithPrefix(
      "router",
      pythonCmd,
      ["-u", "-m", "router.router", "--config", configPath, "--daemon", "--debug"],
      repoRoot
    );
    routerStartedByWeb = true;
  }

  const routerHealthy = await waitForRouterHealthy(routerHost, routerPort, 15000);
  if (!routerHealthy) {
    throw new Error(
      `Router on ${routerHost}:${routerPort} did not become healthy within 15s.`
    );
  }

  // Backend
  const backendPort = 4000;
  const backendAlreadyListening = await isTcpPortListening("127.0.0.1", backendPort);
  if (backendAlreadyListening) {
    const healthy = await checkHttpReady(backendUrl, 1500);
    if (healthy) {
      console.log(`[web] Backend already listening on :${backendPort}; reusing existing process.`);
    } else {
      throw new Error(
        `Backend port ${backendPort} is already in use by an unhealthy process.`
      );
    }
  } else {
    // Use non-watch mode in web stack to avoid restart flapping.
    spawnWithPrefix("backend", "npm", ["run", "web"], backendPath);
  }

  // Frontend
  const frontendPort = 3000;
  const frontendAlreadyListening = await isTcpPortListening("127.0.0.1", frontendPort);
  if (frontendAlreadyListening) {
    const healthy = await checkHttpReady(frontendUrl, 1500);
    if (healthy) {
      console.log(`[web] Frontend already listening on :${frontendPort}; reusing existing process.`);
    } else {
      throw new Error(
        `Frontend port ${frontendPort} is already in use by an unhealthy process.`
      );
    }
  } else {
    spawnWithPrefix("frontend", "npm", ["run", "dev"], frontendPath);
  }

  // Attempt to open browser (best-effort) once services are actually reachable.
  if (!opts.noOpen) {
    const ready = await waitForWebReady(120000);

    if (!ready) {
      console.warn(
        "[web] Warning: Services did not become ready within 120s; skipping automatic browser launch."
      );
    } else {
      try {
        const url = frontendUrl;
        const opener = process.platform === "darwin" ? "open" : "xdg-open";

        const browser = spawn(opener, [url], {
          detached: true,
          stdio: "ignore",
        });

        browser.on("error", () => {
          console.warn("[web] Warning: Could not open browser automatically.");
        });

        browser.unref();
      } catch {
        console.warn("[web] Warning: Could not open browser automatically.");
      }
    }
  }

  let shuttingDown = false;
  async function shutdown() {
    if (shuttingDown) {
      return;
    }
    shuttingDown = true;
    console.log("\n[web] Shutting down...");
    for (const child of children) {
      try {
        child.kill("SIGTERM");
      } catch {}
    }
    if (routerStartedByWeb) {
      try {
        const stopped = await stopRouterProcess({ config: configPath });
        if (!stopped) {
          console.warn("[web] Warning: router stop was requested but no live router pid was found.");
        }
      } catch {
        console.warn("[web] Warning: failed to stop router cleanly.");
      }
    }
    process.exit(0);
  }

  process.on("SIGINT", () => {
    void shutdown();
  });
  process.on("SIGTERM", () => {
    void shutdown();
  });
}

program
  .command("web")
  .description("Run full Codeswarm web stack locally")
  .option("--config <path>", "Path to router config")
  .option("--no-open", "Do not open browser automatically")
  .action(async (cmd: any) => {
    await runWebStack(cmd);
  });


program
  .command("doctor")
  .description("Run environment diagnostics")
  .action(() => {
    runDoctor();
  });

program.parse(process.argv);
