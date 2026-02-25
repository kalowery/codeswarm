import net from "net";
import { ITransport } from "./ITransport.js";

export class TcpTransport implements ITransport {
  private socket: net.Socket | null = null;
  private listeners: ((msg: any) => void)[] = [];
  private buffer = "";
  private connected = false;

  constructor(host: string, port: number) {
    this.connectWithRetry(host, port);
  }

  private connectWithRetry(host: string, port: number, retries = 50) {
    const socket = net.createConnection({ host, port });

    socket.on("connect", () => {
      this.socket = socket;
      this.connected = true;
      this.setupListeners();
    });

    socket.on("error", (err) => {
      socket.destroy();
      if (retries > 0) {
        setTimeout(() => {
          this.connectWithRetry(host, port, retries - 1);
        }, 100);
      } else {
        console.error("Unable to connect to router:", err);
      }
    });
  }

  private setupListeners() {
    if (!this.socket) return;

    this.socket.on("data", (chunk: Buffer) => {
      this.buffer += chunk.toString();

      while (this.buffer.includes("\n")) {
        const idx = this.buffer.indexOf("\n");
        const line = this.buffer.slice(0, idx).trim();
        this.buffer = this.buffer.slice(idx + 1);

        if (!line) continue;

        try {
          const msg = JSON.parse(line);
          this.listeners.forEach(l => l(msg));
        } catch (err) {
          console.error("TCP JSON parse error:", err, line);
        }
      }
    });

    this.socket.on("error", console.error);
  }

  send(message: object) {
    const line = JSON.stringify(message) + "\n";

    const trySend = () => {
      if (this.connected && this.socket) {
        this.socket.write(line);
      } else {
        setTimeout(trySend, 50);
      }
    };

    trySend();
  }

  onMessage(cb: (msg: any) => void) {
    this.listeners.push(cb);
  }

  close() {
    if (this.socket) {
      this.socket.end();
    }
  }
}
