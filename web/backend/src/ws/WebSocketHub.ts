import { WebSocketServer } from 'ws';

export class WebSocketHub {
  constructor(private wss: WebSocketServer) {}

  broadcast(message: any) {
    const data = JSON.stringify(message);
    for (const client of this.wss.clients) {
      if (client.readyState === 1) {
        client.send(data);
      }
    }
  }
}