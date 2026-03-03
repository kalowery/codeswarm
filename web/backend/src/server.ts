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

// --- Helper: request swarm status ---
function requestStatus(swarm_id: string) {
  const request_id = router.send('swarm_status', { swarm_id });
  requestSwarmMap[request_id] = swarm_id;
}

// --- Router Event Handling ---
router.on('event', (msg: any) => {
  console.log('Router event received:', msg.event);
  const { event, data } = msg;

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
    hub.broadcast({ type: 'turn_complete', payload: data });
    return;
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
