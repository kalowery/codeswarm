#!/usr/bin/env node

import { execSync } from "child_process";
import { existsSync, copyFileSync } from "fs";
import { resolve, dirname } from "path";
import { fileURLToPath } from "url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

const rootDir = __dirname;
const cliDir = resolve(rootDir, "cli");
const skillSrc = resolve(rootDir, "integrations/openclaw/SKILL.md");

const openclawDir = process.env.OPENCLAW_DIR || resolve(process.env.HOME || "", "openclaw");
const skillDestDir = resolve(openclawDir, "skills", "codeswarm");

function run(cmd, cwd) {
  console.log(`==> ${cmd}`);
  execSync(cmd, { stdio: "inherit", cwd });
}

try {
  console.log("==> Building Codeswarm CLI");
  run("npm install", cliDir);
  run("npm run build", cliDir);
  run("npm link", cliDir);

  console.log("==> Installing / Updating OpenClaw skill");

  if (existsSync(skillDestDir)) {
    copyFileSync(skillSrc, resolve(skillDestDir, "SKILL.md"));
    console.log(`Updated skill at ${skillDestDir}`);
  } else {
    console.log(`OpenClaw skill directory not found at ${skillDestDir}`);
    console.log("Skipping skill installation.");
  }

  console.log("\n✅ Installation complete.");
  console.log("If OpenClaw is running, restart the gateway:");
  console.log("  openclaw gateway restart");
} catch (err) {
  console.error("Installation failed:");
  console.error(err.message);
  process.exit(1);
}