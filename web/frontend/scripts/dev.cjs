#!/usr/bin/env node

const { spawn } = require('child_process');

if (!process.env.WATCHPACK_POLLING) {
  process.env.WATCHPACK_POLLING = 'true';
}

const nextBin = require.resolve('next/dist/bin/next');
const child = spawn(process.execPath, [nextBin, 'dev'], {
  stdio: 'inherit',
  env: process.env,
});

child.on('exit', (code, signal) => {
  if (signal) {
    process.kill(process.pid, signal);
    return;
  }
  process.exit(code ?? 0);
});
