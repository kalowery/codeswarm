import { CodeswarmClient } from "./codeswarm-client";
export async function handle(ctx) {
    const args = ctx.args || [];
    const sub = args[0];
    const client = new CodeswarmClient();
    await client.connect();
    if (!ctx.session.codeswarm) {
        ctx.session.codeswarm = {};
    }
    const state = ctx.session.codeswarm;
    client.onEvent((msg) => {
        switch (msg.event) {
            case "swarm_launched":
                state.swarmId = msg.data.swarm_id;
                ctx.reply(`✅ Swarm launched: ${state.swarmId}`);
                break;
            case "assistant_delta":
            case "assistant": {
                const node = msg.data.node_id;
                ctx.stream(`[node ${node}] ${msg.data.content}`);
                break;
            }
            case "turn_complete":
                ctx.streamEnd();
                break;
            case "command_rejected":
                ctx.reply(`❌ ${msg.data.reason}`);
                break;
        }
    });
    if (sub === "launch") {
        const nodes = parseInt(args[1], 10);
        client.launch(nodes, "OpenClaw-managed swarm");
        return;
    }
    if (sub === "inject") {
        if (!state.swarmId) {
            ctx.reply("No active swarm.");
            return;
        }
        const content = args.slice(1).join(" ");
        client.inject(state.swarmId, content);
        return;
    }
    if (sub === "status") {
        if (!state.swarmId) {
            ctx.reply("No active swarm.");
            return;
        }
        client.status(state.swarmId);
        return;
    }
    ctx.reply("Usage: /swarm launch <nodes> | inject <prompt> | status");
}
