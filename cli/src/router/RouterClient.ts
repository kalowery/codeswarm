import { v4 as uuidv4 } from "uuid";
import { ITransport } from "./transport/ITransport.js";

export class RouterClient {
  constructor(private transport: ITransport) {}

  private sendCommand(command: string, payload: any) {
    const requestId = uuidv4();

    this.transport.send({
      protocol: "codeswarm.router.v1",
      type: "command",
      command,
      request_id: requestId,
      payload,
    });

    return requestId;
  }

  launch(nodes: number, systemPrompt: string) {
    return this.sendCommand("swarm_launch", {
      nodes,
      system_prompt: systemPrompt,
    });
  }

  listSwarms() {
    return this.sendCommand("swarm_list", {});
  }

  status(swarmId: string) {
    return this.sendCommand("swarm_status", { swarm_id: swarmId });
  }

  inject(swarmId: string, nodes: number | "all", content: string) {
    return this.sendCommand("inject", {
      swarm_id: swarmId,
      nodes,
      content,
    });
  }

  terminate(swarmId: string) {
    return this.sendCommand("swarm_terminate", {
      swarm_id: swarmId,
    });
  }

  onEvent(cb: (msg: any) => void) {
    this.transport.onMessage(cb);
  }
}
