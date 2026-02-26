#!/usr/bin/env node

import { Command } from "commander";
import { TcpTransport } from "./router/transport/TcpTransport.js";
import { spawn } from "child_process";
import { RouterClient } from "./router/RouterClient.js";
import { EventFormatter } from "./formatter/EventFormatter.js";
import path from "path";
import process from "process";
import net from "net";

const program = new Command();

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
    if (!opts.config && !opts.router) {
      console.error("--config required unless --router is provided");
      process.exit(1);
    }

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
    if (!opts.config && !opts.router) {
      console.error("--config required unless --router is provided");
      process.exit(1);
    }

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
  .option("--config <path>", "Path to router config")
  .option("--router <address>", "Router address override (host:port)")
  .option("--debug", "Print raw JSON messages from router", false)
  .action(async (cmd: any) => {
    const opts = cmd;
    if (!opts.config && !opts.router) {
      console.error("--config required unless --router is provided");
      process.exit(1);
    }

    const transport = await createTransport(opts);
    const client = new RouterClient(transport);

    let requestId: string | null = null;

    client.onEvent((e) => {
      if (!requestId || e?.data?.request_id !== requestId) return;

      switch (e.event) {
        case "swarm_launched":
          console.log("Swarm launched successfully:\n");
          Object.entries(e.data).forEach(([k, v]) => {
            console.log(`${k}: ${v}`);
          });
          process.exit(0);
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

    requestId = client.launch(
      parseInt(cmd.nodes),
      cmd.prompt
    );
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
    if (!opts.config && !opts.router) {
      console.error("--config required unless --router is provided");
      process.exit(1);
    }

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

async function createTransport(opts: any) {
  const __dirname = path.dirname(new URL(import.meta.url).pathname);
  const routerPath = path.resolve(__dirname, "../../router/router.py");

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

  const isRouterRunning = (): Promise<boolean> => {
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
  };

  if (!opts.router) {
    const running = await isRouterRunning();

    if (running) {
      console.error("[codeswarm] Using existing router.");
    } else {
      console.error("[codeswarm] Starting embedded router...");

      const routerProcess = spawn(
        "python3",
        ["-u", routerPath, "--config", opts.config, "--daemon"],
        { stdio: "ignore" }
      );

      const shutdown = () => {
        try {
          routerProcess.kill();
        } catch {}
      };

      process.on("exit", shutdown);
      process.on("SIGINT", () => {
        shutdown();
        process.exit(0);
      });
      process.on("SIGTERM", () => {
        shutdown();
        process.exit(0);
      });
    }
  }

  return new TcpTransport(host, port, opts.debug === true);
}


program
  .command("attach <swarmId>")
  .description("Attach to a running swarm and stream events continuously")
  .option("--config <path>", "Path to router config")
  .option("--router <address>", "Router address override (host:port)")
  .option("--debug", "Print raw JSON messages from router", false)
  .action(async (swarmId: string, cmd: any) => {
    const opts = cmd;
    if (!opts.config && !opts.router) {
      console.error("--config required unless --router is provided");
      process.exit(1);
    }

    const transport = await createTransport(opts);
    const client = new RouterClient(transport);

    console.log(`🔗 Attached to swarm ${swarmId}`);
    console.log("Press Ctrl+C to detach.\n");

    client.onEvent((e: any) => {
      if (
        e?.data?.swarm_id === swarmId &&
        typeof e?.data?.node_id === "number"
      ) {
        const node = e.data.node_id;

        if (e.event === "assistant_delta") {
          process.stdout.write(e.data.content);
        }

        if (e.event === "assistant") {
          process.stdout.write(e.data.content);
        }

        if (e.event === "turn_started") {
          process.stdout.write(`\n\n[swarm ${swarmId} | node ${node}]\n`);
        }

        if (e.event === "turn_complete") {
          process.stdout.write("\n");
        }
      }
    });

    process.on("SIGINT", () => {
      console.log("\nDetached.");
      process.exit(0);
    });
  });

program.parse(process.argv);
