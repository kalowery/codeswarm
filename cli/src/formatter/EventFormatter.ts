interface InjectionState {
  content: string;
  tokens?: number;
  nodeId: number;
  swarmId: string;
}

export class EventFormatter {
  private injections = new Map<string, InjectionState>();

  handle(event: any) {
    if (event.type !== "event") return;

    const name = event.event;
    const data = event.data;

    if (!data) return;

    if (name === "swarm_list") {
      console.log("\nAvailable Swarms:");
      console.log("------------------------------------------------------------");
      for (const [id, info] of Object.entries(data.swarms || {})) {
        const s: any = info;
        console.log(`${id} | job ${s.job_id} | nodes ${s.node_count} | ${s.status}`);
      }
      console.log("------------------------------------------------------------\n");
    }

    if (name === "swarm_status") {
      console.log("\nSwarm Status:");
      console.log("------------------------------------------------------------");
      Object.entries(data).forEach(([k, v]) => {
        console.log(`${k}: ${v}`);
      });
      console.log("------------------------------------------------------------\n");
    }

    if (name === "assistant_delta") {
      const key = data.injection_id;
      const prev = this.injections.get(key) ?? {
        content: "",
        nodeId: data.node_id,
        swarmId: data.swarm_id,
      };

      prev.content += data.content;
      this.injections.set(key, prev);
    }

    if (name === "assistant") {
      const key = data.injection_id;
      const state = this.injections.get(key);
      if (!state) return;

      console.log(`\n[Swarm ${state.swarmId} | Node ${state.nodeId}]`);
      console.log("------------------------------------------------------------");
      console.log(state.content);
    }

    if (name === "usage") {
      const key = data.injection_id;
      const state = this.injections.get(key);
      if (!state) return;

      state.tokens = data.total_tokens;
      console.log(`\nTokens used: ${state.tokens}`);
      console.log("------------------------------------------------------------\n");
    }

    if (name === "inject_ack") {
      console.log(`Inject acknowledged (node ${data.node_id})`);
    }

    if (name === "inject_failed") {
      console.error(`Inject failed: ${data.error}`);
    }
  }
}
