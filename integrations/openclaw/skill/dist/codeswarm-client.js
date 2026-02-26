import net from "net";
import { v4 as uuidv4 } from "uuid";
export class CodeswarmClient {
    host;
    port;
    socket;
    buffer = "";
    handlers = [];
    constructor(host = "127.0.0.1", port = 8765) {
        this.host = host;
        this.port = port;
    }
    async connect() {
        return new Promise((resolve, reject) => {
            this.socket = net.createConnection({ host: this.host, port: this.port }, resolve);
            this.socket.on("data", (chunk) => {
                this.buffer += chunk.toString();
                this.processBuffer();
            });
            this.socket.on("error", reject);
        });
    }
    processBuffer() {
        let idx;
        while ((idx = this.buffer.indexOf("\n")) >= 0) {
            const line = this.buffer.slice(0, idx);
            this.buffer = this.buffer.slice(idx + 1);
            if (!line.trim())
                continue;
            const parsed = JSON.parse(line);
            this.handlers.forEach((h) => h(parsed));
        }
    }
    onEvent(handler) {
        this.handlers.push(handler);
    }
    send(command, payload) {
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
    launch(nodes, systemPrompt) {
        return this.send("swarm_launch", {
            nodes,
            system_prompt: systemPrompt,
        });
    }
    inject(swarmId, content) {
        return this.send("inject", {
            swarm_id: swarmId,
            nodes: "all",
            content,
        });
    }
    status(swarmId) {
        return this.send("swarm_status", {
            swarm_id: swarmId,
        });
    }
    list() {
        return this.send("swarm_list", {});
    }
}
