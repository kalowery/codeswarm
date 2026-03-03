import express from 'express'
import cors from 'cors';
import { WebSocketServer } from 'ws';
import http from 'http';
import { RouterBridge } from './router/RouterBridge';
import { SwarmStateManager } from './state/SwarmStateManager';
import { WebSocketHub } from './ws/WebSocketHub';

const app = express();

app.use(cors({
  origin: true,
  credentials: true
}));
app.use(express.json());

const server = http.createServer(app);
const wss = new WebSocketServer({ server });

const router = new RouterBridge();
const state = new SwarmStateManager();
const hub = new WebSocketHub(wss);

// Track pending aliases keyed by launch request_id
const pendingAliases: Record<string, string | undefined> = {};

// Track request_id -> swarm_id for status/inject/terminate
const requestSwarmMap: Record<string, string> = {};
let interSwarmQueueItems: any[] = [];
const requestPromptMap = new Map<string, string>();
const injectionPromptMap = new Map<string, string>();
const processedAutoRoutes = new Set<string>();

type AutoRouteDirective =
  | { targetAlias: string; mode: 'idle'; prompt: string }
  | { targetAlias: string; mode: 'all'; prompt: string }
  | { targetAlias: string; mode: 'nodes'; prompt: string; nodes: number[] };

function parseNodeSpec(expr: string): number[] {
  const resolved = new Set<number>();
  expr.split(',').forEach((part) => {
    const chunk = part.trim();
    if (!chunk) return;
    if (/^\d+$/.test(chunk)) {
      resolved.add(Number(chunk));
      return;
    }
    const rangeMatch = chunk.match(/^(\d+)\s*-\s*(\d+)$/);
    if (!rangeMatch) return;
    const start = Number(rangeMatch[1]);
    const end = Number(rangeMatch[2]);
    if (start > end) return;
    for (let i = start; i <= end; i += 1) {
      resolved.add(i);
    }
  });
  return Array.from(resolved);
}

function extractText(value: any): string {
  if (typeof value === 'string') return value;
  if (Array.isArray(value)) return value.map((v) => extractText(v)).join('');
  if (!value || typeof value !== 'object') return '';

  if (typeof value.text === 'string') return value.text;
  if (typeof value.message === 'string') return value.message;
  if (typeof value.output_text === 'string') return value.output_text;
  if (typeof value.content === 'string') return value.content;

  if (Array.isArray(value.content)) return extractText(value.content);
  if (Array.isArray(value.parts)) return extractText(value.parts);
  if (Array.isArray(value.messages)) return extractText(value.messages);

  return '';
}

function parseAutoRouteDirectives(text: string): AutoRouteDirective[] {
  const directives: AutoRouteDirective[] = [];
  const lines = text.split(/\r?\n/);

  for (const rawLine of lines) {
    const line = rawLine.trim();
    if (!line.startsWith('/swarm[')) continue;

    const crossAllMatch = line.match(/^\/swarm\[(.+?)\]\/all\s+([\s\S]+)$/);
    if (crossAllMatch) {
      const targetAlias = (crossAllMatch[1] ?? '').trim();
      const prompt = (crossAllMatch[2] ?? '').trim();
      if (targetAlias && prompt) {
        directives.push({ targetAlias, mode: 'all', prompt });
      }
      continue;
    }

    const crossIdleMatch = line.match(/^\/swarm\[(.+?)\]\/(idle|first-idle)\s+([\s\S]+)$/);
    if (crossIdleMatch) {
      const targetAlias = (crossIdleMatch[1] ?? '').trim();
      const prompt = (crossIdleMatch[3] ?? '').trim();
      if (targetAlias && prompt) {
        directives.push({ targetAlias, mode: 'idle', prompt });
      }
      continue;
    }

    const crossNodeMatch = line.match(/^\/swarm\[(.+?)\]\/node\[(.+?)\]\s*([\s\S]+)$/);
    if (crossNodeMatch) {
      const targetAlias = (crossNodeMatch[1] ?? '').trim();
      const expr = (crossNodeMatch[2] ?? '').trim();
      const prompt = (crossNodeMatch[3] ?? '').trim();
      const nodes = parseNodeSpec(expr);
      if (targetAlias && prompt && nodes.length > 0) {
        directives.push({ targetAlias, mode: 'nodes', prompt, nodes });
      }
    }
  }

  return directives;
}

function handleAutoRoutingFromTaskComplete(data: any) {
  const injectionId = typeof data?.injection_id === 'string' ? data.injection_id : '';
  if (!injectionId || processedAutoRoutes.has(injectionId)) return;
  processedAutoRoutes.add(injectionId);

  const sourceSwarmId = typeof data?.swarm_id === 'string' ? data.swarm_id : '';
  if (!sourceSwarmId) return;

  const sourceSwarm = state.getById(sourceSwarmId);
  if (!sourceSwarm) return;

  const finalText = extractText(data?.last_agent_message).trim();
  if (!finalText) return;

  const directives = parseAutoRouteDirectives(finalText);
  if (directives.length === 0) return;

  for (const directive of directives) {
    const targetSwarm = state.getByAlias(directive.targetAlias);
    if (!targetSwarm) {
      hub.broadcast({
        type: 'auto_route_ignored',
        payload: {
          source_swarm_id: sourceSwarmId,
          source_alias: sourceSwarm.alias,
          target_alias: directive.targetAlias,
          reason: 'unknown target alias',
          injection_id: injectionId,
          mode: directive.mode
        }
      });
      continue;
    }

    if (directive.mode === 'idle') {
      const request_id = router.send('enqueue_inject', {
        source_swarm_id: sourceSwarmId,
        target_swarm_id: targetSwarm.swarm_id,
        selector: 'idle',
        content: directive.prompt
      });
      requestSwarmMap[request_id] = targetSwarm.swarm_id;
      requestPromptMap.set(request_id, directive.prompt);
      hub.broadcast({
        type: 'auto_route_submitted',
        payload: {
          request_id,
          source_swarm_id: sourceSwarmId,
          source_alias: sourceSwarm.alias,
          target_swarm_id: targetSwarm.swarm_id,
          target_alias: targetSwarm.alias,
          selector: 'idle',
          injection_id: injectionId
        }
      });
      continue;
    }

    const request_id = router.send('inject', {
      swarm_id: targetSwarm.swarm_id,
      nodes: directive.mode === 'all' ? 'all' : directive.nodes,
      content: directive.prompt
    });
    requestSwarmMap[request_id] = targetSwarm.swarm_id;
    requestPromptMap.set(request_id, directive.prompt);
    hub.broadcast({
      type: 'auto_route_submitted',
      payload: {
        request_id,
        source_swarm_id: sourceSwarmId,
        source_alias: sourceSwarm.alias,
        target_swarm_id: targetSwarm.swarm_id,
        target_alias: targetSwarm.alias,
        selector: directive.mode,
        nodes: directive.mode === 'nodes' ? directive.nodes : undefined,
        injection_id: injectionId
      }
    });
  }
}

// --- Helper: request swarm status ---
function requestStatus(swarm_id: string) {
  const request_id = router.send('swarm_status', { swarm_id });
  requestSwarmMap[request_id] = swarm_id;
}

// --- Router Event Handling ---
router.on('event', (msg: any) => {
  console.log('Router event received:', msg.event);
  const { event, data } = msg;

  if (event === 'inject_ack') {
    const prompt = typeof data?.request_id === 'string' ? requestPromptMap.get(data.request_id) : undefined;
    if (prompt && typeof data?.injection_id === 'string') {
      data.prompt = prompt;
      injectionPromptMap.set(data.injection_id, prompt);
    }
  }

  if (event === 'swarm_launched') {
    const { swarm_id, job_id, node_count, request_id } = data;

    // Use user-provided alias if available
    const alias = pendingAliases[request_id] || `swarm-${swarm_id.slice(0, 8)}`;
    delete pendingAliases[request_id];

    const record = state.createSwarm(swarm_id, alias, job_id, node_count);

    // Initially mark as pending until Slurm confirms
    state.updateStatus(swarm_id, 'pending');

    hub.broadcast({ type: 'swarm_added', payload: record });

    // Immediately request real Slurm status
    requestStatus(swarm_id);
  }

  // --- Core conversational events (explicit mapping for backwards compatibility) ---
  if (event === 'turn_started') {
    if (
      (!data?.prompt || typeof data.prompt !== 'string') &&
      typeof data?.injection_id === 'string'
    ) {
      const prompt = injectionPromptMap.get(data.injection_id);
      if (prompt) {
        data.prompt = prompt;
      }
    }
    hub.broadcast({ type: 'turn_started', payload: data });
    return;
  }

  if (event === 'assistant_delta') {
    hub.broadcast({ type: 'delta', payload: data });
    return;
  }

  if (event === 'assistant') {
    hub.broadcast({ type: 'assistant', payload: data });
    return;
  }

  if (event === 'turn_complete') {
    if (typeof data?.injection_id === 'string') {
      injectionPromptMap.delete(data.injection_id);
    }
    hub.broadcast({ type: 'turn_complete', payload: data });
    return;
  }

  if (event === 'task_complete') {
    if (typeof data?.injection_id === 'string') {
      injectionPromptMap.delete(data.injection_id);
    }
    handleAutoRoutingFromTaskComplete(data);
  }

  // Handle router rejections
  if (event === 'command_rejected') {
    const swarm_id = requestSwarmMap[data.request_id];

    if (data.reason === 'unknown swarm_id' && swarm_id) {
      console.warn('Removing unknown swarm from backend state:', swarm_id);
      state.remove(swarm_id);
      hub.broadcast({ type: 'swarm_removed', payload: { swarm_id } });
    }

    delete requestSwarmMap[data.request_id];
    hub.broadcast({ type: 'command_rejected', payload: data });
    return;
  }

  if (event === 'queue_updated') {
    interSwarmQueueItems = Array.isArray(data?.items) ? data.items : [];
    // Continue with generic passthrough below.
  }

  // --- Generic passthrough for all other router events ---
  // This keeps backend decoupled from router protocol evolution.
  hub.broadcast({ type: event, payload: data });

  if (event === 'swarm_list') {
    // Full authoritative reconciliation from router
    const swarms = data.swarms || {};

    // Remove stale swarms not present in router
    for (const existing of state.list()) {
      if (!swarms[existing.swarm_id]) {
        state.remove(existing.swarm_id);
      }
    }

    // Add or update swarms from router
    for (const swarm_id of Object.keys(swarms)) {
      const swarm = swarms[swarm_id];
      if (!state.getById(swarm_id)) {
        const alias = `swarm-${swarm_id.slice(0, 8)}`;
        state.createSwarm(
          swarm_id,
          alias,
          swarm.job_id,
          swarm.node_count
        );
      }
    }

    hub.broadcast({ type: 'reconcile', payload: state.list() });
    return;
  }

  if (event === 'swarm_status') {
    const { swarm_id, status, slurm_state } = data;

    // Do not treat NOT_FOUND as authoritative termination
    if (slurm_state === 'NOT_FOUND') {
      // Preserve existing status, only update slurm_state
      state.updateStatus(swarm_id, undefined as any, slurm_state);
    } else {
      state.updateStatus(swarm_id, status, slurm_state);
    }

    hub.broadcast({ type: 'status', payload: data });
  }

  if (event === 'swarm_terminated') {
    state.remove(data.swarm_id);
    hub.broadcast({ type: 'swarm_removed', payload: data });
    return;
  }

  // Handle router-driven removal (TTL prune or other cleanup)
  if (event === 'swarm_removed') {
    state.remove(data.swarm_id);
    hub.broadcast({ type: 'swarm_removed', payload: data });
    return;
  }
});

// --- Periodic Status Refresh ---
// SLURM POLL LOOP DISABLED (2026-02-28)
// Reason: SSH squeue polling is causing lifecycle flapping and false terminations.
// Status will now be driven by explicit router lifecycle events only.
// setInterval(() => {
//   for (const swarm of state.list()) {
//     if (swarm.status !== 'terminated') {
//       requestStatus(swarm.swarm_id);
//     }
//   }
// }, 5000); // every 5 seconds

// --- REST Endpoints ---
app.post('/launch', (req, res) => {
  const { nodes, prompt, alias } = req.body;

  const request_id = router.send('swarm_launch', {
    nodes,
    system_prompt: prompt
  });

  // Store alias temporarily until swarm_launched event arrives
  pendingAliases[request_id] = alias;

  res.json({ request_id });
});

app.post('/inject/:alias', (req, res) => {
  const swarm = state.getByAlias(req.params.alias);
  if (!swarm) return res.status(404).json({ error: 'Unknown swarm' });

  const { prompt, nodes, target_alias, selector } = req.body;

  if (!prompt || typeof prompt !== 'string') {
    return res.status(400).json({ error: 'Invalid prompt' });
  }

  // Cross-swarm routing mode: frontend can request that prompt be delivered
  // to another swarm with selector semantics (e.g. first idle node).
  if (typeof target_alias === 'string' && target_alias.trim()) {
    const targetSwarm = state.getByAlias(target_alias);
    if (!targetSwarm) return res.status(404).json({ error: 'Unknown target swarm' });

    const mode = typeof selector === 'string' ? selector : 'idle';

    if (mode === 'idle' || mode === 'first-idle') {
      const request_id = router.send('enqueue_inject', {
        source_swarm_id: swarm.swarm_id,
        target_swarm_id: targetSwarm.swarm_id,
        selector: 'idle',
        content: prompt
      });
      requestSwarmMap[request_id] = targetSwarm.swarm_id;
      requestPromptMap.set(request_id, prompt);
      return res.json({ request_id });
    }

    const targets: number[] = Array.isArray(nodes)
      ? nodes
          .map((n: any) => Number(n))
          .filter((n: number) => !isNaN(n) && n >= 0 && n < targetSwarm.node_count)
      : Array.from({ length: targetSwarm.node_count }, (_, i) => i);

    const payloadTargets =
      targets.length === targetSwarm.node_count ? 'all' : targets;

    const request_id = router.send('inject', {
      swarm_id: targetSwarm.swarm_id,
      nodes: payloadTargets,
      content: prompt
    });
    requestSwarmMap[request_id] = targetSwarm.swarm_id;
    requestPromptMap.set(request_id, prompt);
    return res.json({ request_id });
  }

  const targets: number[] = Array.isArray(nodes)
    ? nodes
        .map((n: any) => Number(n))
        .filter((n: number) => !isNaN(n) && n >= 0 && n < swarm.node_count)
    : Array.from({ length: swarm.node_count }, (_, i) => i);

  const payloadTargets =
    targets.length === swarm.node_count ? "all" : targets;

  const request_id = router.send('inject', {
    swarm_id: swarm.swarm_id,
    nodes: payloadTargets,
    content: prompt
  });

  requestSwarmMap[request_id] = swarm.swarm_id;
  requestPromptMap.set(request_id, prompt);

  res.json({ request_id });
});

app.post('/terminate/:alias', (req, res) => {
  const swarm = state.getByAlias(req.params.alias);
  if (!swarm) return res.status(404).json({ error: 'Unknown swarm' });

  const request_id = router.send('swarm_terminate', { swarm_id: swarm.swarm_id });
  requestSwarmMap[request_id] = swarm.swarm_id;

  res.json({ request_id });
});

app.get('/swarms', (req, res) => {
  res.json(state.list());
});

app.get('/queue', (req, res) => {
  res.json(interSwarmQueueItems);
});

app.post('/approval', (req, res) => {
  const { job_id, call_id, approved, decision } = req.body;

  if (!job_id || !call_id) {
    return res.status(400).json({ error: 'Missing job_id or call_id' });
  }

  const normalizedApproved =
    typeof approved === 'boolean'
      ? approved
      : decision === 'abort'
      ? false
      : true;

  const request_id = router.send('approve_execution', {
    job_id,
    call_id,
    approved: normalizedApproved,
    decision
  });

  res.json({ request_id });
});

async function connectWithRetry() {
  while (true) {
    try {
      await router.connect();
      console.log('Connected to router');

      // Initial reconciliation
      router.send('swarm_list', {});
      router.send('queue_list', {});

      break;
    } catch (err) {
      console.log('Router not ready, retrying in 2s...');
      await new Promise(res => setTimeout(res, 2000));
    }
  }
}

(async () => {
  await connectWithRetry();
  const PORT = 4000;
  server.listen(PORT, () => {
    console.log(`Backend running on http://localhost:${PORT}`);
  });
})();
