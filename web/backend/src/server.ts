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

// --- Helper: request swarm status ---
function requestStatus(swarm_id: string) {
  router.send('swarm_status', { swarm_id });
}

// --- Router Event Handling ---
router.on('event', (msg: any) => {
  console.log('Router event received:', msg.event);
  const { event, data } = msg;

  if (event === 'swarm_launched') {
    const { swarm_id, job_id, node_count } = data;
    const alias = `swarm-${swarm_id.slice(0, 8)}`;
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

  // --- Generic passthrough for all other router events ---
  // This keeps backend decoupled from router protocol evolution.
  hub.broadcast({ type: event, payload: data });

  if (event === 'swarm_list') {
    // Full state reconciliation
    const swarms = data.swarms || {};
    for (const swarm_id of Object.keys(swarms)) {
      if (!state.getById(swarm_id)) {
        const swarm = swarms[swarm_id];
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
  }

  if (event === 'swarm_status') {
    state.updateStatus(data.swarm_id, data.status, data.slurm_state);
    hub.broadcast({ type: 'status', payload: data });
  }

  if (event === 'swarm_terminated') {
    state.remove(data.swarm_id);
    hub.broadcast({ type: 'swarm_removed', payload: data });
  }
});

// --- Periodic Status Refresh ---
setInterval(() => {
  for (const swarm of state.list()) {
    if (swarm.status !== 'terminated') {
      requestStatus(swarm.swarm_id);
    }
  }
}, 5000); // every 5 seconds

// --- REST Endpoints ---
app.post('/launch', (req, res) => {
  const { nodes, prompt } = req.body;
  const request_id = router.send('swarm_launch', { nodes, system_prompt: prompt });
  res.json({ request_id });
});

app.post('/inject/:alias', (req, res) => {
  const swarm = state.getByAlias(req.params.alias);
  if (!swarm) return res.status(404).json({ error: 'Unknown swarm' });

  const { prompt } = req.body;
  const request_id = router.send('inject', {
    swarm_id: swarm.swarm_id,
    nodes: 'all',
    content: prompt
  });

  res.json({ request_id });
});

app.post('/terminate/:alias', (req, res) => {
  const swarm = state.getByAlias(req.params.alias);
  if (!swarm) return res.status(404).json({ error: 'Unknown swarm' });

  const request_id = router.send('swarm_terminate', { swarm_id: swarm.swarm_id });
  res.json({ request_id });
});

app.get('/swarms', (req, res) => {
  res.json(state.list());
});

async function connectWithRetry() {
  while (true) {
    try {
      await router.connect();
      console.log('Connected to router');

      // Initial reconciliation
      router.send('swarm_list', {});

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