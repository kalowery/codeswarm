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

  launch(
    nodes: number,
    systemPrompt: string,
    opts?: {
      provider?: string;
      providerParams?: Record<string, string | number | boolean>;
      agentsMdContent?: string;
      agentsBundle?: {
        mode: "file" | "directory";
        agents_md_content: string;
        skills_files: Array<{ path: string; content: string }>;
      };
    }
  ) {
    const payload: any = {
      nodes,
      system_prompt: systemPrompt,
    };

    if (typeof opts?.provider === "string" && opts.provider.trim()) {
      payload.provider = opts.provider.trim();
    }

    if (
      typeof opts?.agentsMdContent === "string" &&
      opts.agentsMdContent.trim()
    ) {
      payload.agents_md_content = opts.agentsMdContent;
    }

    if (opts?.agentsBundle) {
      payload.agents_bundle = opts.agentsBundle;
    }

    if (
      opts?.providerParams &&
      Object.keys(opts.providerParams).length > 0
    ) {
      payload.provider_params = opts.providerParams;
    }

    return this.sendCommand("swarm_launch", payload);
  }

  providers() {
    return this.sendCommand("providers_list", {});
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

  projectResume(
    projectId: string,
    opts?: {
      workerSwarmIds?: string[];
      retryFailed?: boolean;
      reverifyCompleted?: boolean;
    }
  ) {
    const payload: any = {
      project_id: projectId,
    };

    if (Array.isArray(opts?.workerSwarmIds)) {
      payload.worker_swarm_ids = opts.workerSwarmIds;
    }
    if (typeof opts?.retryFailed === "boolean") {
      payload.retry_failed = opts.retryFailed;
    }
    if (typeof opts?.reverifyCompleted === "boolean") {
      payload.reverify_completed = opts.reverifyCompleted;
    }

    return this.sendCommand("project_resume", payload);
  }

  projectResumePreview(
    projectId: string,
    opts?: {
      workerSwarmIds?: string[];
      retryFailed?: boolean;
      reverifyCompleted?: boolean;
    }
  ) {
    const payload: any = {
      project_id: projectId,
    };

    if (Array.isArray(opts?.workerSwarmIds)) {
      payload.worker_swarm_ids = opts.workerSwarmIds;
    }
    if (typeof opts?.retryFailed === "boolean") {
      payload.retry_failed = opts.retryFailed;
    }
    if (typeof opts?.reverifyCompleted === "boolean") {
      payload.reverify_completed = opts.reverifyCompleted;
    }

    return this.sendCommand("project_resume_preview", payload);
  }

  onEvent(cb: (msg: any) => void) {
    this.transport.onMessage(cb);
  }
}
