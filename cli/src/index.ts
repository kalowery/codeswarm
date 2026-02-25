#!/usr/bin/env node

import { Command } from "commander";
import { TcpTransport } from "./router/transport/TcpTransport.js";
import { spawn } from "child_process";
import { RouterClient } from "./router/RouterClient.js";
import { EventFormatter } from "./formatter/EventFormatter.js";
import path from "path";
import process from "process";

const program = new Command();

program
  .name("codeswarm")
  .description("Codeswarm CLI")
  .option("--config <path>", "Path to router config")
  .option("--router <address>", "Router address override (future TCP)");

program
  .command("list")
  .description("List available swarms")
  .action(async () => {
    const opts = program.opts();
    if (!opts.config && !opts.router) {
      console.error("--config required unless --router is provided");
      process.exit(1);
    }

    const transport = createTransport(opts);
    const client = new RouterClient(transport);
    const formatter = new EventFormatter();

    let requestId: string | null = null;

    client.onEvent((e) => {
      if (requestId && e?.data?.request_id === requestId) {
        formatter.handle(e);
        process.exit(0);
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

    const transport = createTransport(opts);
    const client = new RouterClient(transport);
    const formatter = new EventFormatter();

    let requestId: string | null = null;

    client.onEvent((e) => {
      if (requestId && e?.data?.request_id === requestId) {
        formatter.handle(e);
        process.exit(0);
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

    const transport = createTransport(opts);
    const client = new RouterClient(transport);

    let requestId: string | null = null;

    client.onEvent((e) => {
      if (requestId && e?.data?.request_id === requestId) {
        console.log("Swarm launched successfully:\n");
        Object.entries(e.data).forEach(([k, v]) => {
          console.log(`${k}: ${v}`);
        });
        process.exit(0);
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

    const transport = createTransport(opts);
    const client = new RouterClient(transport);
    const formatter = new EventFormatter();

    client.onEvent((e) => formatter.handle(e));

    const nodes = cmd.nodes === "all" ? "all" : parseInt(cmd.nodes);
    client.inject(swarmId, nodes as any, cmd.prompt);
  });

function createTransport(opts: any) {
  const __dirname = path.dirname(new URL(import.meta.url).pathname);
  const routerPath = path.resolve(__dirname, "../../router/router.py");

  if (!opts.router) {
    // spawn router detached
    spawn("python3", [
      "-u",
      routerPath,
      "--config",
      opts.config,
      "--daemon"
    ], {
      detached: true,
      stdio: "ignore"
    });
  }

  return new TcpTransport("127.0.0.1", 8765);
}

program.parse(process.argv);
