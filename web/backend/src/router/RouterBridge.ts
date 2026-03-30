import net from 'net';
import { EventEmitter } from 'events';
import { randomUUID } from 'crypto';

export interface RouterEvent {
  event: string;
  data: any;
}

export class RouterBridge extends EventEmitter {
  private socket?: net.Socket;
  private buffer = '';
  private connected = false;

  constructor(
    private host = process.env.CODESWARM_ROUTER_HOST || '127.0.0.1',
    private port = Number(process.env.CODESWARM_ROUTER_PORT || 8765)
  ) {
    super();
  }

  connect(): Promise<void> {
    return new Promise((resolve, reject) => {
      this.socket = net.createConnection({ host: this.host, port: this.port }, () => {
        this.connected = true;
        this.emit('connected');
        resolve();
      });

      this.socket.on('data', (chunk) => {
        this.buffer += chunk.toString();
        let lines = this.buffer.split('\n');
        this.buffer = lines.pop() || '';
        for (const line of lines) {
          if (!line.trim()) continue;
          try {
            const msg = JSON.parse(line);
            if (msg.type === 'event') {
              this.emit('event', msg);
            }
          } catch {}
        }
      });

      this.socket.on('close', () => {
        this.connected = false;
        this.emit('disconnected');
      });

      this.socket.on('error', (err) => {
        this.connected = false;
        reject(err);
      });
    });
  }

  isConnected(): boolean {
    return Boolean(this.connected && this.socket && !this.socket.destroyed);
  }

  send(command: string, payload: any): string {
    if (!this.isConnected()) {
      throw new Error('Router connection unavailable');
    }
    const request_id = randomUUID();
    const envelope = {
      protocol: 'codeswarm.router.v1',
      type: 'command',
      command,
      request_id,
      payload
    };
    const serialized = JSON.stringify(envelope);
    console.log('Sending to router:', serialized);
    this.socket!.write(serialized + '\n');
    return request_id;
  }
}
