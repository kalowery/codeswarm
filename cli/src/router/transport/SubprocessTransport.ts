import { spawn } from "child_process";
import { ITransport } from "./ITransport.js";

export class SubprocessTransport implements ITransport {
  private proc;
  private listeners: ((msg: any) => void)[] = [];
  private ready = false;
  private queue: string[] = [];

constructor(command: string, args: string[]) {
  console.log("Spawning router with:", command, args);

  this.proc = spawn(command, args, {
    stdio: ['pipe', 'pipe', 'pipe']
  });

  console.log("Spawned PID:", this.proc.pid);

  this.proc.stdin.setDefaultEncoding('utf8');

  this.proc.on('error', console.error);
  this.proc.stdin.on('error', console.error);

  this.proc.on('spawn', () => {
    this.ready = true;
    for (const msg of this.queue) {
      this.proc.stdin.write(msg);
    }
    this.queue = [];
    // close stdin after sending initial command (one-shot CLI)
    this.proc.stdin.end();
  });

  this.proc.stdout.on('data', (chunk: Buffer) => {
    const lines = chunk.toString().split('\n');
    for (const line of lines) {
      if (!line.trim()) continue;
      try {
        const msg = JSON.parse(line);
        this.listeners.forEach(l => l(msg));
      } catch {}
    }
  });

  // Forward router stderr for debugging
  this.proc.stderr.on('data', (chunk: Buffer) => {
    console.error("ROUTER STDERR:", chunk.toString());
  });
}
send(message: object) {
  const line = JSON.stringify(message) + "\n";

  if (this.ready) {
    this.proc.stdin.write(line);
  } else {
    this.queue.push(line);
  }
}

  onMessage(cb: (msg: any) => void) {
    this.listeners.push(cb);
  }

  close() {
    this.proc.kill();
  }
}
