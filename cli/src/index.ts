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
  .option("--config <path>", "Path to router config")
  .option("--router <address>", "Router address override (future TCP)")
  .option("--debug", "Print raw JSON messages from router", false);

program
  .command("list")
  .description("List available swarms")
  .action(async () => {
    const opts = program.opts();
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
  .action(async (swarmId: string) => {
    const opts = program.opts();
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
  .requiredOption("--partition <partition>", "Slurm partition")
  .requiredOption("--time <time>", "Time limit (e.g. 00:10:00)")
  .requiredOption("--prompt <text>", "System prompt")
  .action(async (cmd: any) => {
    const opts = program.opts();
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
      cmd.partition,
      cmd.time,
      cmd.prompt
    );
  });

program
  .command("inject <swarmId>")
  .description("Inject prompt into swarm")
  .requiredOption("--prompt <text>", "Prompt text")
  .option("--nodes <nodes>", "Node index or 'all'", "all")
  .action(async (swarmId: string, cmd: any) => {
    const opts = program.opts();
    if (!opts.config && !opts.router) {
      console.error("--config required unless --router is provided");
      process.exit(1);
    }

    const transport = await createTransport(opts);
    const client = new RouterClient(transport);
    const formatter = new EventFormatter();

    client.onEvent((e) => formatter.handle(e));

    // Step 1: Fetch swarm status to determine node count
    const statusRequestId = client.status(swarmId);

    let nodeCount: number | null = null;

    await new Promise<void>((resolve) => {
      client.onEvent((e) => {
        if (e?.data?.request_id === statusRequestId) {
          nodeCount = e?.data?.node_count ?? null;
          resolve();
        }
      });
    });

    if (nodeCount === null) {
      console.error("Unable to determine node count for swarm.");
      process.exit(1);
    }

    // Step 2: Resolve target nodes
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

    // Step 3: Inject per node
    for (const nodeIdx of targetNodes) {
      client.inject(swarmId, nodeIdx, cmd.prompt);
    }
  });

async function createTransport(opts: any) {
  const __dirname = path.dirname(new URL(import.meta.url).pathname);
  const routerPath = path.resolve(__dirname, "../../router/router.py");

  const host = "127.0.0.1";
  const port = 8765;

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

program.parse(process.argv);
