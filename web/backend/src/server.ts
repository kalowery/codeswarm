import express from 'express'
import cors from 'cors';
import { WebSocketServer } from 'ws';
import http from 'http';
import fs from 'fs';
import path from 'path';
import { randomUUID } from 'crypto';
import { RouterBridge } from './router/RouterBridge';
import { SwarmStateManager } from './state/SwarmStateManager';
import { WebSocketHub } from './ws/WebSocketHub';

const app = express();

app.use(cors({
  origin: true,
  credentials: true
}));
app.use(express.json({ limit: '2mb' }));

const server = http.createServer(app);
const wss = new WebSocketServer({ server });

const router = new RouterBridge();
const state = new SwarmStateManager();
// Router is authoritative for active swarms; drop backend persisted ghosts.
state.clearAll();
const hub = new WebSocketHub(wss);
let routerReconnectInProgress = false;

wss.on('connection', (ws) => {
  try {
    ws.send(
      JSON.stringify({
        type: 'approvals_snapshot',
        payload: clonePendingApprovalsSnapshot()
      })
    );
  } catch {
    // Ignore one-off socket send failures; normal broadcast path will continue.
  }
});

// Track pending aliases keyed by launch request_id
const pendingAliases: Record<string, string | undefined> = {};
const pendingLaunchTimers = new Map<string, NodeJS.Timeout>();

// Track request_id -> swarm_id for status/inject/terminate
const requestSwarmMap: Record<string, string> = {};
let interSwarmQueueItems: any[] = [];
const requestPromptMap = new Map<string, string>();
const injectionPromptMap = new Map<string, string>();
const processedAutoRoutes = new Set<string>();
const workspaceDownloads = new Map<string, { archivePath: string; archiveName: string; createdAt: number }>();
let launchProviders: any[] = [];
const pendingProvidersRequests = new Map<
  string,
  { resolve: (providers: any[]) => void; reject: (error: Error) => void; timer: NodeJS.Timeout }
>();
type ApprovalStatus =
  | 'pending'
  | 'submitted'
  | 'acknowledged'
  | 'started'
  | 'resolved'
  | 'rejected'
  | 'timeout';
type ApprovalRecord = {
  job_id?: string;
  call_id: string;
  injection_id?: string;
  created_at_ms: number;
  updated_at_ms: number;
  approval_seq: number;
  status: ApprovalStatus;
  submit_attempts?: number;
  last_request_id?: string;
  command?: any;
  reason?: string;
  cwd?: string;
  proposed_execpolicy_amendment?: any;
  available_decisions?: any;
};
const pendingApprovalsBySwarm = new Map<string, Map<number, ApprovalRecord[]>>();
const approvalKeyByRequestId = new Map<string, string>();
let approvalSeq = 0;

function sendRouterCommand(command: string, payload: any): string {
  if (!router.isConnected()) {
    throw new Error('Router is disconnected');
  }
  return router.send(command, payload);
}
type ApprovalAckType = 'resolved' | 'started' | 'rejected' | 'timeout';
type ApprovalCriteria = { job_id: string; call_id: string; node_id?: number };
type ApprovalAck = { type: ApprovalAckType; reason?: string; request_id?: string };
const approvalAckWaiters = new Map<
  string,
  {
    criteria: ApprovalCriteria;
    timer: NodeJS.Timeout;
    resolve: (ack: ApprovalAck) => void;
  }
>();

function normalizeNodeId(value: any): number | undefined {
  const n = Number(value);
  return Number.isFinite(n) ? n : undefined;
}

function logApprovalTrace(stage: string, payload: Record<string, any>) {
  try {
    console.log(
      '[backend APPROVAL]',
      JSON.stringify({
        stage,
        ts: new Date().toISOString(),
        ...payload
      })
    );
  } catch {
    // Best-effort diagnostics only.
  }
}

function matchApprovalCriteria(data: any, criteria: ApprovalCriteria): boolean {
  if (!data) return false;
  if (String(data.job_id || '') !== String(criteria.job_id || '')) return false;
  if (String(data.call_id || '') !== String(criteria.call_id || '')) return false;
  if (Number.isFinite(criteria.node_id)) {
    const eventNode = normalizeNodeId(data.node_id);
    if (!Number.isFinite(eventNode) || eventNode !== criteria.node_id) return false;
  }
  return true;
}

function resolveApprovalAckByRequestId(requestId: string, ack: ApprovalAck) {
  const waiter = approvalAckWaiters.get(requestId);
  if (!waiter) return;
  clearTimeout(waiter.timer);
  approvalAckWaiters.delete(requestId);
  waiter.resolve(ack);
}

function resolveApprovalAcksByData(data: any, ack: ApprovalAck) {
  for (const [requestId, waiter] of approvalAckWaiters.entries()) {
    if (!matchApprovalCriteria(data, waiter.criteria)) continue;
    clearTimeout(waiter.timer);
    approvalAckWaiters.delete(requestId);
    waiter.resolve({ ...ack, request_id: requestId });
  }
}

function waitForApprovalAck(
  requestId: string,
  criteria: ApprovalCriteria,
  timeoutMs: number
): Promise<ApprovalAck> {
  return new Promise((resolve) => {
    const timer = setTimeout(() => {
      approvalAckWaiters.delete(requestId);
      resolve({ type: 'timeout', request_id: requestId });
    }, timeoutMs);
    approvalAckWaiters.set(requestId, { criteria, timer, resolve });
  });
}

function clonePendingApprovalsSnapshot() {
  const snapshot: Record<string, Record<number, ApprovalRecord[]>> = {};
  for (const [swarmId, byNode] of pendingApprovalsBySwarm.entries()) {
    const nodeMap: Record<number, ApprovalRecord[]> = {};
    for (const [nodeId, approvals] of byNode.entries()) {
      if (!Array.isArray(approvals) || approvals.length === 0) continue;
      const active = approvals.filter((a) =>
        a?.status === 'pending' || a?.status === 'submitted' || a?.status === 'acknowledged'
      );
      if (active.length === 0) continue;
      nodeMap[nodeId] = active.map((a) => ({ ...a }));
    }
    if (Object.keys(nodeMap).length === 0) continue;
    snapshot[swarmId] = nodeMap;
  }
  return snapshot;
}

function broadcastApprovalsSnapshot() {
  hub.broadcast({
    type: 'approvals_snapshot',
    payload: clonePendingApprovalsSnapshot()
  });
}

function approvalKey(jobId: string, nodeId: number, callId: string) {
  return `${jobId}:${nodeId}:${callId}`;
}

function nextApprovalSeq() {
  approvalSeq += 1;
  return approvalSeq;
}

function upsertPendingApproval(swarmId: string, nodeId: number, approval: any) {
  if (!swarmId || !Number.isFinite(nodeId)) return;
  if (!approval || typeof approval.call_id !== 'string' || !approval.call_id) return;
  const byNode = pendingApprovalsBySwarm.get(swarmId) ?? new Map<number, ApprovalRecord[]>();
  const existing = byNode.get(nodeId) ?? [];
  const idx = existing.findIndex((a) => a?.call_id === approval.call_id);
  const now = Date.now();
  const nextApproval = {
    ...approval,
    status: 'pending' as ApprovalStatus,
    updated_at_ms: now,
    approval_seq: nextApprovalSeq(),
    created_at_ms:
      typeof existing[idx]?.created_at_ms === 'number'
        ? existing[idx].created_at_ms
        : now
  } as ApprovalRecord;
  const next = idx >= 0
    ? existing.map((a, i) => (i === idx ? nextApproval : a))
    : [...existing, nextApproval];
  byNode.set(nodeId, next);
  pendingApprovalsBySwarm.set(swarmId, byNode);
}

function findApprovalRecord(jobId: string, callId: string, nodeId?: number) {
  for (const [swarmId, byNode] of pendingApprovalsBySwarm.entries()) {
    for (const [currentNodeId, approvals] of byNode.entries()) {
      if (Number.isFinite(nodeId) && currentNodeId !== nodeId) continue;
      const idx = approvals.findIndex(
        (a) => String(a.call_id) === String(callId) && String(a.job_id || '') === String(jobId || '')
      );
      if (idx >= 0) {
        return { swarmId, nodeId: currentNodeId, byNode, approvals, idx, approval: approvals[idx] };
      }
    }
  }
  return undefined;
}

function transitionApprovalStatus(
  params: { jobId: string; callId: string; nodeId?: number; requestId?: string },
  status: ApprovalStatus,
  patch: Partial<ApprovalRecord> = {}
) {
  const found = findApprovalRecord(params.jobId, params.callId, params.nodeId);
  if (!found) return false;
  const now = Date.now();
  const updated: ApprovalRecord = {
    ...found.approval,
    ...patch,
    status,
    updated_at_ms: now,
    approval_seq: nextApprovalSeq()
  };
  found.approvals[found.idx] = updated;
  found.byNode.set(found.nodeId, found.approvals);
  pendingApprovalsBySwarm.set(found.swarmId, found.byNode);
  if (params.requestId) {
    approvalKeyByRequestId.set(
      params.requestId,
      approvalKey(String(params.jobId), Number(found.nodeId), String(params.callId))
    );
  }
  return true;
}

function removePendingApprovalByCallId(swarmId: string, callId: string, nodeId?: number) {
  if (!swarmId || !callId) return;
  const byNode = pendingApprovalsBySwarm.get(swarmId);
  if (!byNode) return;
  const nodeIds = Number.isFinite(nodeId) ? [Number(nodeId)] : Array.from(byNode.keys());
  for (const id of nodeIds) {
    const existing = byNode.get(id) ?? [];
    const next = existing.filter((a) => a?.call_id !== callId);
    if (next.length > 0) byNode.set(id, next);
    else byNode.delete(id);
  }
  if (byNode.size === 0) pendingApprovalsBySwarm.delete(swarmId);
  for (const [requestId, key] of approvalKeyByRequestId.entries()) {
    const keyParts = key.split(':');
    const keyCallId = keyParts.length >= 3 ? keyParts.slice(2).join(':') : '';
    const keyNodeId = keyParts.length >= 2 ? Number(keyParts[1]) : NaN;
    if (keyCallId !== String(callId)) continue;
    if (Number.isFinite(nodeId) && Number.isFinite(keyNodeId) && keyNodeId !== Number(nodeId)) continue;
    approvalKeyByRequestId.delete(requestId);
  }
}

function clearPendingApprovalsForSwarm(swarmId: string) {
  if (!swarmId) return;
  pendingApprovalsBySwarm.delete(swarmId);
  approvalKeyByRequestId.clear();
}

type AutoRouteDirective =
  | { targetAlias?: string; mode: 'idle'; prompt: string }
  | { targetAlias?: string; mode: 'all'; prompt: string }
  | { targetAlias?: string; mode: 'nodes'; prompt: string; nodes: number[] };

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
  const startRegex = /^(?:\s*(?:[-*+]|\d+\.)\s+|\s*>\s+)?(\/(?:swarm\[[^\]\r\n]+\]\/(?:all|idle|first-idle|(?:agent|node)\[[^\]\r\n]+\])|all|(?:agent|node)\[[^\]\r\n]+\]))(?:\s+|$)/gm;
  const commands: Array<{ command: string; lineStart: number; bodyStart: number }> = [];

  for (const match of text.matchAll(startRegex)) {
    const raw = match[0] ?? '';
    const command = (match[1] ?? '').trim();
    const lineStart = match.index ?? 0;
    commands.push({
      command,
      lineStart,
      bodyStart: lineStart + raw.length
    });
  }

  for (let i = 0; i < commands.length; i += 1) {
    const current = commands[i];
    const next = commands[i + 1];
    const prompt = text
      .slice(current.bodyStart, next ? next.lineStart : text.length)
      .trim();
    if (!prompt) continue;

    const command = current.command;

    if (command.startsWith('/swarm[')) {
      const crossAllMatch = command.match(/^\/swarm\[(.+?)\]\/all$/);
      if (crossAllMatch) {
        const targetAlias = (crossAllMatch[1] ?? '').trim();
        if (targetAlias) directives.push({ targetAlias, mode: 'all', prompt });
        continue;
      }

      const crossIdleMatch = command.match(/^\/swarm\[(.+?)\]\/(idle|first-idle)$/);
      if (crossIdleMatch) {
        const targetAlias = (crossIdleMatch[1] ?? '').trim();
        if (targetAlias) directives.push({ targetAlias, mode: 'idle', prompt });
        continue;
      }

      const crossAgentMatch = command.match(/^\/swarm\[(.+?)\]\/(?:agent|node)\[(.+?)\]$/);
      if (crossAgentMatch) {
        const targetAlias = (crossAgentMatch[1] ?? '').trim();
        const expr = (crossAgentMatch[2] ?? '').trim();
        const nodes = parseNodeSpec(expr);
        if (targetAlias && nodes.length > 0) {
          directives.push({ targetAlias, mode: 'nodes', prompt, nodes });
        }
      }
      continue;
    }

    if (command === '/all') {
      directives.push({ mode: 'all', prompt });
      continue;
    }

    const localAgentMatch = command.match(/^\/(?:agent|node)\[(.+?)\]$/);
    if (localAgentMatch) {
      const expr = (localAgentMatch[1] ?? '').trim();
      const nodes = parseNodeSpec(expr);
      if (nodes.length > 0) {
        directives.push({ mode: 'nodes', prompt, nodes });
      }
    }
  }

  return directives;
}

function handleAutoRoutingFromFinalText(data: any, finalText: string) {
  const injectionId = typeof data?.injection_id === 'string' ? data.injection_id : '';
  if (!injectionId || processedAutoRoutes.has(injectionId)) return;

  const sourceSwarmId = typeof data?.swarm_id === 'string' ? data.swarm_id : '';
  if (!sourceSwarmId) return;

  const sourceSwarm = state.getById(sourceSwarmId);
  if (!sourceSwarm) return;

  const normalizedText = finalText.trim();
  if (!normalizedText) return;

  const directives = parseAutoRouteDirectives(normalizedText);
  if (directives.length === 0) return;
  processedAutoRoutes.add(injectionId);

  for (const directive of directives) {
    const targetSwarm = directive.targetAlias
      ? state.getByAlias(directive.targetAlias)
      : sourceSwarm;
    if (!targetSwarm) {
      hub.broadcast({
        type: 'auto_route_ignored',
        payload: {
          source_swarm_id: sourceSwarmId,
          source_alias: sourceSwarm.alias,
          target_alias: directive.targetAlias ?? sourceSwarm.alias,
          reason: 'unknown target alias',
          injection_id: injectionId,
          mode: directive.mode
        }
      });
      continue;
    }

    try {
      const request_id = sendRouterCommand('enqueue_inject', {
        source_swarm_id: sourceSwarmId,
        target_swarm_id: targetSwarm.swarm_id,
        selector: directive.mode,
        nodes: directive.mode === 'nodes' ? directive.nodes : undefined,
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
    } catch {
      hub.broadcast({
        type: 'auto_route_ignored',
        payload: {
          source_swarm_id: sourceSwarmId,
          source_alias: sourceSwarm.alias,
          target_swarm_id: targetSwarm.swarm_id,
          target_alias: targetSwarm.alias,
          reason: 'router unavailable',
          injection_id: injectionId,
          mode: directive.mode
        }
      });
    }
  }
}

function handleAutoRoutingFromTaskComplete(data: any) {
  const finalText = extractText(data?.last_agent_message);
  handleAutoRoutingFromFinalText(data, finalText);
}

function requestProvidersCatalog(timeoutMs = 5000): Promise<any[]> {
  return new Promise((resolve, reject) => {
    let request_id = '';
    try {
      request_id = sendRouterCommand('providers_list', {});
    } catch (err) {
      reject(err instanceof Error ? err : new Error('Router unavailable'));
      return;
    }
    const timer = setTimeout(() => {
      pendingProvidersRequests.delete(request_id);
      reject(new Error('Timed out waiting for providers_list'));
    }, timeoutMs);
    pendingProvidersRequests.set(request_id, { resolve, reject, timer });
  });
}

function fallbackProvidersCatalog(): any[] {
  return [
    { id: 'local', label: 'LOCAL', backend: 'local', defaults: {}, launch_fields: [], launch_panels: [] },
    { id: 'slurm', label: 'SLURM', backend: 'slurm', defaults: {}, launch_fields: [], launch_panels: [] },
    { id: 'aws', label: 'AWS', backend: 'aws', defaults: {}, launch_fields: [], launch_panels: [] }
  ];
}

// --- Helper: request swarm status ---
function requestStatus(swarm_id: string) {
  try {
    const request_id = sendRouterCommand('swarm_status', { swarm_id });
    requestSwarmMap[request_id] = swarm_id;
  } catch {
    // Ignore opportunistic status refresh while router is unavailable.
  }
}

function clearPendingLaunchTimer(requestId: string) {
  const timer = pendingLaunchTimers.get(requestId);
  if (!timer) return;
  clearTimeout(timer);
  pendingLaunchTimers.delete(requestId);
}

function startPendingLaunchTimer(requestId: string, timeoutMs = 120000) {
  clearPendingLaunchTimer(requestId);
  const timer = setTimeout(() => {
    pendingLaunchTimers.delete(requestId);
    delete pendingAliases[requestId];
    delete requestSwarmMap[requestId];
    hub.broadcast({
      type: 'command_rejected',
      payload: {
        request_id: requestId,
        reason: `launch timed out after ${Math.floor(timeoutMs / 1000)}s`
      }
    });
  }, timeoutMs);
  pendingLaunchTimers.set(requestId, timer);
}

// --- Router Event Handling ---
router.on('event', (msg: any) => {
  console.log('Router event received:', msg.event);
  const { event, data } = msg;

  if (event === 'exec_approval_required') {
    const swarmId = typeof data?.swarm_id === 'string' ? data.swarm_id : '';
    const nodeId = Number(data?.node_id);
    const jobId = typeof data?.job_id === 'string' ? data.job_id : '';
    if (swarmId && Number.isFinite(nodeId)) {
      upsertPendingApproval(swarmId, nodeId, {
        job_id: jobId,
        call_id: typeof data?.call_id === 'string' ? data.call_id : '',
        injection_id: typeof data?.injection_id === 'string' ? data.injection_id : undefined,
        command: data?.command,
        reason: typeof data?.reason === 'string' ? data.reason : '',
        cwd: typeof data?.cwd === 'string' ? data.cwd : undefined,
        proposed_execpolicy_amendment: Array.isArray(data?.proposed_execpolicy_amendment)
          ? data.proposed_execpolicy_amendment
          : undefined,
        available_decisions: Array.isArray(data?.available_decisions)
          ? data.available_decisions
          : undefined
      });
      logApprovalTrace('router_exec_approval_required', {
        swarm_id: swarmId,
        job_id: jobId,
        node_id: nodeId,
        call_id: typeof data?.call_id === 'string' ? data.call_id : '',
        turn_id: typeof data?.turn_id === 'string' ? data.turn_id : '',
        injection_id: typeof data?.injection_id === 'string' ? data.injection_id : ''
      });
      broadcastApprovalsSnapshot();
    }
  }

  if (event === 'exec_approval_resolved') {
    logApprovalTrace('router_exec_approval_resolved', {
      request_id: typeof data?.request_id === 'string' ? data.request_id : '',
      swarm_id: typeof data?.swarm_id === 'string' ? data.swarm_id : '',
      job_id: typeof data?.job_id === 'string' ? data.job_id : '',
      node_id: Number.isFinite(Number(data?.node_id)) ? Number(data.node_id) : undefined,
      call_id: typeof data?.call_id === 'string' ? data.call_id : '',
      approved: typeof data?.approved === 'boolean' ? data.approved : undefined
    });
    if (typeof data?.job_id === 'string' && typeof data?.call_id === 'string') {
      transitionApprovalStatus(
        {
          jobId: data.job_id,
          callId: data.call_id,
          nodeId: Number.isFinite(Number(data?.node_id)) ? Number(data.node_id) : undefined
        },
        'resolved'
      );
    }
    resolveApprovalAcksByData(data, { type: 'resolved' });
    const swarmId =
      typeof data?.swarm_id === 'string'
        ? data.swarm_id
        : typeof data?.job_id === 'string'
        ? state.list().find((s) => s.job_id === data.job_id)?.swarm_id ?? ''
        : '';
    const callId = typeof data?.call_id === 'string' ? data.call_id : '';
    const nodeId = Number(data?.node_id);
    if (swarmId && callId) {
      removePendingApprovalByCallId(swarmId, callId, Number.isFinite(nodeId) ? nodeId : undefined);
      broadcastApprovalsSnapshot();
    }
  }

  if (event === 'command_started') {
    if (typeof data?.call_id === 'string' && data.call_id) {
      logApprovalTrace('router_command_started', {
        request_id: typeof data?.request_id === 'string' ? data.request_id : '',
        swarm_id: typeof data?.swarm_id === 'string' ? data.swarm_id : '',
        job_id: typeof data?.job_id === 'string' ? data.job_id : '',
        node_id: Number.isFinite(Number(data?.node_id)) ? Number(data.node_id) : undefined,
        call_id: data.call_id
      });
      if (typeof data?.job_id === 'string') {
        transitionApprovalStatus(
          {
            jobId: data.job_id,
            callId: data.call_id,
            nodeId: Number.isFinite(Number(data?.node_id)) ? Number(data.node_id) : undefined
          },
          'started'
        );
      }
      resolveApprovalAcksByData(data, { type: 'started' });
    }
    const swarmId = typeof data?.swarm_id === 'string' ? data.swarm_id : '';
    const callId = typeof data?.call_id === 'string' ? data.call_id : '';
    const nodeId = Number(data?.node_id);
    if (swarmId && callId) {
      removePendingApprovalByCallId(swarmId, callId, Number.isFinite(nodeId) ? nodeId : undefined);
      broadcastApprovalsSnapshot();
    }
  }

  if (event === 'command_rejected') {
    const requestId = typeof data?.request_id === 'string' ? data.request_id : '';
    logApprovalTrace('router_command_rejected', {
      request_id: requestId,
      reason: typeof data?.reason === 'string' ? data.reason : '',
      job_id: typeof data?.job_id === 'string' ? data.job_id : '',
      node_id: Number.isFinite(Number(data?.node_id)) ? Number(data.node_id) : undefined,
      call_id: typeof data?.call_id === 'string' ? data.call_id : ''
    });
    if (requestId) clearPendingLaunchTimer(requestId);
    if (requestId && approvalKeyByRequestId.has(requestId)) {
      const key = approvalKeyByRequestId.get(requestId)!;
      approvalKeyByRequestId.delete(requestId);
      const [jobId, nodeIdRaw, callId] = key.split(':');
      const parsedNode = Number(nodeIdRaw);
      transitionApprovalStatus(
        { jobId, callId, nodeId: Number.isFinite(parsedNode) ? parsedNode : undefined },
        'rejected'
      );
      broadcastApprovalsSnapshot();
    }
    if (requestId) {
      resolveApprovalAckByRequestId(requestId, {
        type: 'rejected',
        reason: typeof data?.reason === 'string' ? data.reason : 'command rejected',
        request_id: requestId
      });
    }
  }

  if (event === 'inject_ack') {
    const prompt = typeof data?.request_id === 'string' ? requestPromptMap.get(data.request_id) : undefined;
    if (prompt && typeof data?.injection_id === 'string') {
      data.prompt = prompt;
      injectionPromptMap.set(data.injection_id, prompt);
    }
  }

  if (event === 'swarm_launched') {
    const { swarm_id, job_id, node_count, request_id, provider, provider_id } = data;
    if (typeof request_id === 'string' && request_id) {
      clearPendingLaunchTimer(request_id);
    }

    // Use user-provided alias if available
    const alias = pendingAliases[request_id] || `swarm-${swarm_id.slice(0, 8)}`;
    delete pendingAliases[request_id];

    const record = state.createSwarm(swarm_id, alias, job_id, node_count, { provider, provider_id });

    // Initially mark as pending until Slurm confirms
    state.updateStatus(swarm_id, 'pending');

    hub.broadcast({ type: 'swarm_added', payload: record });

    // Immediately request real Slurm status
    requestStatus(swarm_id);
  }

  if (event === 'providers_list') {
    const providers = Array.isArray(data?.providers) ? data.providers : [];
    launchProviders = providers;
    const requestId = typeof data?.request_id === 'string' ? data.request_id : '';
    if (requestId && pendingProvidersRequests.has(requestId)) {
      const pending = pendingProvidersRequests.get(requestId)!;
      clearTimeout(pending.timer);
      pendingProvidersRequests.delete(requestId);
      pending.resolve(providers);
    }
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
    // Some runtimes emit final assistant text without a task_complete payload.
    // Parse assistant snapshots as a fallback for /-prefixed auto-routing.
    const assistantText = extractText(data?.content);
    handleAutoRoutingFromFinalText(data, assistantText);
    hub.broadcast({ type: 'assistant', payload: data });
    return;
  }

  if (event === 'workspace_archive_ready') {
    const archivePath = typeof data?.archive_path === 'string' ? data.archive_path : '';
    const archiveName = typeof data?.archive_name === 'string' ? data.archive_name : path.basename(archivePath || 'workspace.tar.gz');
    if (!archivePath || !fs.existsSync(archivePath)) {
      hub.broadcast({
        type: 'workspace_archive_failed',
        payload: {
          ...data,
          reason: 'Workspace archive was reported but is not accessible on backend host'
        }
      });
      return;
    }

    const token = randomUUID();
    workspaceDownloads.set(token, {
      archivePath,
      archiveName,
      createdAt: Date.now()
    });
    hub.broadcast({
      type: 'workspace_archive_ready',
      payload: {
        ...data,
        download_url: `/downloads/${token}`
      }
    });
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
    if (typeof data?.request_id === 'string') {
      clearPendingLaunchTimer(data.request_id);
    }
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
          swarm.node_count,
          { provider: swarm.provider, provider_id: swarm.provider_id }
        );
      } else {
        state.updateProviderMeta(swarm_id, swarm.provider, swarm.provider_id);
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
    clearPendingApprovalsForSwarm(data.swarm_id);
    broadcastApprovalsSnapshot();
    state.remove(data.swarm_id);
    hub.broadcast({ type: 'swarm_removed', payload: data });
    return;
  }

  // Handle router-driven removal (TTL prune or other cleanup)
  if (event === 'swarm_removed') {
    clearPendingApprovalsForSwarm(data.swarm_id);
    broadcastApprovalsSnapshot();
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
setInterval(() => {
  if (!router.isConnected()) return;
  try {
    sendRouterCommand('swarm_list', {});
    sendRouterCommand('queue_list', {});
  } catch {
    // Best-effort reconciliation only.
  }
}, 5000);

// --- REST Endpoints ---
app.post('/launch', (req, res) => {
  const { nodes, prompt, alias, agents_md_content, agents_bundle, provider, provider_params } = req.body;
  const agentsContent =
    typeof agents_md_content === 'string' && agents_md_content.trim().length > 0
      ? agents_md_content
      : undefined;
  const normalizedAgentsBundle =
    agents_bundle && typeof agents_bundle === 'object' ? agents_bundle : undefined;
  const normalizedProvider = typeof provider === 'string' && provider.trim().length > 0 ? provider : undefined;
  const normalizedProviderParams = provider_params && typeof provider_params === 'object' ? provider_params : undefined;

  let request_id = '';
  try {
    request_id = sendRouterCommand('swarm_launch', {
      nodes,
      system_prompt: prompt,
      agents_md_content: agentsContent,
      agents_bundle: normalizedAgentsBundle,
      provider: normalizedProvider,
      provider_params: normalizedProviderParams
    });
  } catch (err) {
    return res.status(503).json({ error: 'Router unavailable. Try again in a moment.' });
  }

  // Store alias temporarily until swarm_launched event arrives
  pendingAliases[request_id] = alias;
  startPendingLaunchTimer(request_id);

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
      let request_id = '';
      try {
        request_id = sendRouterCommand('enqueue_inject', {
          source_swarm_id: swarm.swarm_id,
          target_swarm_id: targetSwarm.swarm_id,
          selector: 'idle',
          content: prompt
        });
      } catch {
        return res.status(503).json({ error: 'Router unavailable. Try again in a moment.' });
      }
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

    let request_id = '';
    try {
      request_id = sendRouterCommand('inject', {
        swarm_id: targetSwarm.swarm_id,
        nodes: payloadTargets,
        content: prompt
      });
    } catch {
      return res.status(503).json({ error: 'Router unavailable. Try again in a moment.' });
    }
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

  let request_id = '';
  try {
    request_id = sendRouterCommand('inject', {
      swarm_id: swarm.swarm_id,
      nodes: payloadTargets,
      content: prompt
    });
  } catch {
    return res.status(503).json({ error: 'Router unavailable. Try again in a moment.' });
  }

  requestSwarmMap[request_id] = swarm.swarm_id;
  requestPromptMap.set(request_id, prompt);

  res.json({ request_id });
});

app.post('/terminate/:alias', (req, res) => {
  const swarm = state.getByAlias(req.params.alias);
  if (!swarm) return res.status(404).json({ error: 'Unknown swarm' });
  const downloadWorkspaces = Boolean(req.body?.download_workspaces_on_shutdown);
  let request_id = '';
  try {
    request_id = sendRouterCommand('swarm_terminate', {
      swarm_id: swarm.swarm_id,
      terminate_params: downloadWorkspaces
        ? { download_workspaces_on_shutdown: true }
        : undefined
    });
  } catch (err) {
    return res.status(503).json({ error: 'Router unavailable. Try again in a moment.' });
  }
  requestSwarmMap[request_id] = swarm.swarm_id;

  res.json({ request_id });
});

app.get('/downloads/:token', (req, res) => {
  const token = String(req.params.token || '').trim();
  const record = workspaceDownloads.get(token);
  if (!record) {
    return res.status(404).json({ error: 'Unknown download token' });
  }
  if (!fs.existsSync(record.archivePath)) {
    workspaceDownloads.delete(token);
    return res.status(404).json({ error: 'Archive not found' });
  }
  res.download(record.archivePath, record.archiveName, (err) => {
    if (!err) {
      workspaceDownloads.delete(token);
    }
  });
});

app.get('/swarms', (req, res) => {
  res.json(state.list());
});

app.get('/queue', (req, res) => {
  res.json(interSwarmQueueItems);
});

app.get('/approvals', (req, res) => {
  res.json(clonePendingApprovalsSnapshot());
});

app.get('/providers', async (req, res) => {
  try {
    let providers = await requestProvidersCatalog();
    if (!Array.isArray(providers) || providers.length === 0) {
      providers = await requestProvidersCatalog(8000);
    }
    if (!Array.isArray(providers) || providers.length === 0) {
      if (launchProviders.length > 0) {
        return res.json(launchProviders);
      }
      return res.json(fallbackProvidersCatalog());
    }
    return res.json(providers);
  } catch (err) {
    console.warn('providers_list failed, using fallback catalog:', err);
    if (launchProviders.length > 0) {
      return res.json(launchProviders);
    }
    return res.json(fallbackProvidersCatalog());
  }
});

app.post('/approval', async (req, res) => {
  const { job_id, call_id, node_id, injection_id, approved, decision } = req.body;

  if (!job_id || !call_id) {
    return res.status(400).json({ error: 'Missing job_id or call_id' });
  }

  const normalizedApproved =
    typeof approved === 'boolean'
      ? approved
      : decision === 'abort'
      ? false
      : true;

  const criteriaNodeId = normalizeNodeId(node_id);
  const criteria: ApprovalCriteria = Number.isFinite(criteriaNodeId)
    ? {
        job_id: String(job_id),
        call_id: String(call_id),
        node_id: criteriaNodeId
      }
    : {
        job_id: String(job_id),
        call_id: String(call_id)
      };

  const timeoutsMs = [1200, 1800, 4000];
  let lastRequestId = '';
  logApprovalTrace('ui_submit_received', {
    job_id: String(job_id),
    call_id: String(call_id),
    node_id: criteriaNodeId,
    injection_id: typeof injection_id === 'string' ? injection_id : undefined,
    approved: normalizedApproved,
    decision: decision ?? null
  });

  for (let attempt = 0; attempt < timeoutsMs.length; attempt += 1) {
    let request_id = '';
    try {
      request_id = sendRouterCommand('approve_execution', {
        job_id,
        call_id,
        node_id,
        injection_id,
        approved: normalizedApproved,
        decision
      });
    } catch {
      return res.status(503).json({ error: 'Router unavailable. Try again in a moment.' });
    }
    lastRequestId = request_id;
    logApprovalTrace('router_approve_execution_sent', {
      request_id,
      attempt: attempt + 1,
      job_id: String(job_id),
      call_id: String(call_id),
      node_id: criteria.node_id,
      approved: normalizedApproved,
      decision: decision ?? null
    });
    transitionApprovalStatus(
      {
        jobId: String(job_id),
        callId: String(call_id),
        nodeId: criteria.node_id,
        requestId: request_id
      },
      'submitted',
      { submit_attempts: attempt + 1, last_request_id: request_id }
    );
    broadcastApprovalsSnapshot();

    const timeoutMs = timeoutsMs[attempt] ?? 4000;
    const ack = await waitForApprovalAck(request_id, criteria, timeoutMs);
    logApprovalTrace('router_approve_execution_ack', {
      request_id,
      attempt: attempt + 1,
      ack_type: ack.type,
      ack_reason: ack.reason ?? null,
      ack_request_id: ack.request_id ?? null,
      job_id: String(job_id),
      call_id: String(call_id),
      node_id: criteria.node_id
    });
    if (ack.type === 'resolved' || ack.type === 'started') {
      transitionApprovalStatus(
        {
          jobId: String(job_id),
          callId: String(call_id),
          nodeId: criteria.node_id,
          requestId: request_id
        },
        'acknowledged',
        { last_request_id: request_id }
      );
      broadcastApprovalsSnapshot();
      return res.json({
        request_id,
        status: ack.type,
        attempts: attempt + 1
      });
    }
    if (ack.type === 'rejected') {
      transitionApprovalStatus(
        {
          jobId: String(job_id),
          callId: String(call_id),
          nodeId: criteria.node_id,
          requestId: request_id
        },
        'rejected',
        { last_request_id: request_id }
      );
      broadcastApprovalsSnapshot();
      return res.status(409).json({
        request_id,
        error: ack.reason || 'Approval rejected'
      });
    }
  }

  transitionApprovalStatus(
    {
      jobId: String(job_id),
      callId: String(call_id),
      nodeId: criteria.node_id,
      requestId: lastRequestId
    },
    'timeout',
    { last_request_id: lastRequestId }
  );
  broadcastApprovalsSnapshot();
  return res.status(504).json({
    request_id: lastRequestId,
    error: 'Timed out waiting for approval acknowledgement'
  });
});

async function connectWithRetry() {
  if (routerReconnectInProgress) return;
  routerReconnectInProgress = true;
  while (true) {
    try {
      await router.connect();
      console.log('Connected to router');

      // Initial reconciliation
      sendRouterCommand('swarm_list', {});
      sendRouterCommand('queue_list', {});

      routerReconnectInProgress = false;
      break;
    } catch (err) {
      console.log('Router not ready, retrying in 2s...');
      await new Promise(res => setTimeout(res, 2000));
    }
  }
}

router.on('disconnected', () => {
  console.log('Router disconnected; reconnecting...');
  connectWithRetry().catch(() => {});
});

(async () => {
  await connectWithRetry();
  const PORT = 4000;
  server.listen(PORT, () => {
    console.log(`Backend running on http://localhost:${PORT}`);
  });
})();
