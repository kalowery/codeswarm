#!/usr/bin/env node

import fs from "fs";
import path from "path";
import process from "process";
import { spawnSync } from "child_process";
import { fileURLToPath, pathToFileURL } from "url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const distEntry = path.join(__dirname, "dist", "index.js");
const srcRoot = path.join(__dirname, "src");

function newestMtimeMs(rootPath) {
  if (!fs.existsSync(rootPath)) {
    return 0;
  }
  const stat = fs.statSync(rootPath);
  if (stat.isFile()) {
    return stat.mtimeMs;
  }
  if (!stat.isDirectory()) {
    return 0;
  }
  let newest = stat.mtimeMs;
  for (const entry of fs.readdirSync(rootPath, { withFileTypes: true })) {
    newest = Math.max(newest, newestMtimeMs(path.join(rootPath, entry.name)));
  }
  return newest;
}

function needsBuild() {
  if (!fs.existsSync(distEntry)) {
    return true;
  }
  const distStat = fs.statSync(distEntry);
  const sourceMtime = newestMtimeMs(srcRoot);
  return sourceMtime > distStat.mtimeMs;
}

function ensureBuilt() {
  if (!needsBuild()) {
    return;
  }
  const npmCmd = process.platform === "win32" ? "npm.cmd" : "npm";
  const result = spawnSync(npmCmd, ["run", "build"], {
    cwd: __dirname,
    stdio: "inherit",
    env: process.env,
  });
  if (result.status !== 0) {
    process.exit(result.status ?? 1);
  }
}

ensureBuilt();
await import(pathToFileURL(distEntry).href);
