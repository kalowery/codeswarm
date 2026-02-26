import net from "net";
import { v4 as uuidv4 } from "uuid";

interface RouterMessage {
  protocol: string;
  type: "event";
  timestamp: string;
  event: string;
  data: any;
}

type EventHandler = (msg: RouterMessage) => void;

export class CodeswarmClient {
  private socket!: net.Socket;
  private buffer = "";
  private handlers: EventHandler[] = [];

  constructor(
    private host: string = "127.0.0.1",
    private port: number = 8765
  ) {}

  async connect(): Promise<void> {
    return new Promise((resolve, reject) => {
      this.socket = net.createConnection(
        { host: this.host, port: this.port },
        resolve
      );

      this.socket.on("data", (chunk) => {
        this.buffer += chunk.toString();
        this.processBuffer();
      });

      this.socket.on("error", reject);
    });
  }

  private processBuffer() {
    let idx;
    while ((idx = this.buffer.indexOf("\n")) >= 0) {
      const line = this.buffer.slice(0, idx);
      this.buffer = this.buffer.slice(idx + 1);
      if (!line.trim()) continue;

      const parsed: RouterMessage = JSON.parse(line);
      this.handlers.forEach((h) => h(parsed));
    }
  }

  onEvent(handler: EventHandler) {
    this.handlers.push(handler);
  }

  private send(command: string, payload: any): string {
    const request_id = uuidv4();

    const msg = {
      protocol: "codeswarm.router.v1",
      type: "command",
      timestamp: new Date().toISOString(),
      command,
      request_id,
      payload,
    };

    this.socket.write(JSON.stringify(msg) + "\n");
    return request_id;
  }

  launch(nodes: number, systemPrompt: string) {
    return this.send("swarm_launch", {
      nodes,
      system_prompt: systemPrompt,
    });
  }

  inject(swarmId: string, content: string) {
    return this.send("inject", {
      swarm_id: swarmId,
      nodes: "all",
      content,
    });
  }

  status(swarmId: string) {
    return this.send("swarm_status", {
      swarm_id: swarmId,
    });
  }

  list() {
    return this.send("swarm_list", {});
  }
}
