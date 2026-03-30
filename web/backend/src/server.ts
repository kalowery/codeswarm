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

wss.on('connection', (ws: any) => {
  try {
    ws.send(
      JSON.stringify({
        type: 'approvals_snapshot',
        payload: buildApprovalsSnapshotPayload()
      })
    );
  } catch {
    // Ignore one-off socket send failures; normal broadcast path will continue.
  }
});

// Track pending aliases keyed by launch request_id
const pendingAliases: Record<string, string | undefined> = {};
const pendingLaunchTimers = new Map<
  string,
  { soft: NodeJS.Timeout; hard: NodeJS.Timeout; startedAt: number; softTimeoutMs: number; hardTimeoutMs: number }
>();
const abandonedLaunchRequests = new Set<string>();

// Track request_id -> swarm_id for status/inject/terminate
const requestSwarmMap: Record<string, string> = {};
let interSwarmQueueItems: any[] = [];
const requestPromptMap = new Map<string, string>();
const injectionPromptMap = new Map<string, string>();
let projectsCache: Record<string, any> = {};
const processedAutoRoutes = new Set<string>();
const pendingReplyRoutesByRequestId = new Map<
  string,
  {
    sourceSwarmId: string;
    sourceAlias: string;
    sourceNodeId: number;
    targetSwarmId: string;
    targetAlias: string;
    sourceInjectionId: string;
  }
>();
const replyRoutesByInjectionId = new Map<
  string,
  {
    sourceSwarmId: string;
    sourceAlias: string;
    sourceNodeId: number;
    targetSwarmId: string;
    targetAlias: string;
    targetNodeId: number;
    sourceInjectionId: string;
  }
>();
const processedReplyRoutes = new Set<string>();
const workspaceDownloads = new Map<string, { archivePath: string; archiveName: string; createdAt: number }>();
let launchProviders: any[] = [];
const pendingProvidersRequests = new Map<
  string,
  { resolve: (providers: any[]) => void; reject: (error: Error) => void; timer: NodeJS.Timeout }
>();
const pendingProjectResumePreviewRequests = new Map<
  string,
  { resolve: (preview: any) => void; reject: (error: Error) => void; timer: NodeJS.Timeout }
>();
const pendingApprovalsRequests = new Map<
  string,
  {
    resolve: (snapshot: {
      approvals_version: number;
      approvals: Record<string, Record<number, ApprovalRecord[]>>;
    }) => void;
    reject: (error: Error) => void;
    timer: NodeJS.Timeout;
  }
>();
let approvalsSyncTimer: NodeJS.Timeout | null = null;
let approvalsSyncInFlight = false;
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
  approval_id?: string;
  approval_status?: string;
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
let latestApprovalsVersion = 0;
const waitingOnApprovalByNode = new Map<string, number>();
const WAITING_APPROVAL_STALE_MS = 45_000;
const implicitResolvedApprovalByKey = new Map<string, number>();
const IMPLICIT_RESOLVED_APPROVAL_TTL_MS = 5 * 60_000;
const approvalKeyByRequestId = new Map<string, string>();
let approvalSeq = 0;

function waitingNodeKey(swarmId: string, nodeId: number) {
  return `${swarmId}:${nodeId}`;
}

function approvalNodeKey(swarmId: string, nodeId: number, callId: string) {
  return `${swarmId}:${nodeId}:${callId}`;
}

function markNodeWaitingOnApproval(swarmId: string, nodeId: number) {
  if (!swarmId || !Number.isFinite(nodeId)) return;
  waitingOnApprovalByNode.set(waitingNodeKey(swarmId, Number(nodeId)), Date.now());
}

function clearNodeWaitingOnApproval(swarmId: string, nodeId: number) {
  if (!swarmId || !Number.isFinite(nodeId)) return;
  waitingOnApprovalByNode.delete(waitingNodeKey(swarmId, Number(nodeId)));
}

function pruneStaleWaitingNodes(now: number) {
  for (const [key, ts] of waitingOnApprovalByNode.entries()) {
    if (now - ts > WAITING_APPROVAL_STALE_MS) {
      waitingOnApprovalByNode.delete(key);
    }
  }
}

function pruneStaleImplicitResolvedApprovals(now: number) {
  for (const [key, ts] of implicitResolvedApprovalByKey.entries()) {
    if (now - ts > IMPLICIT_RESOLVED_APPROVAL_TTL_MS) {
      implicitResolvedApprovalByKey.delete(key);
    }
  }
}

function markImplicitApprovalResolved(swarmId: string, nodeId: number, callId: string) {
  if (!swarmId || !Number.isFinite(nodeId) || !callId) return;
  implicitResolvedApprovalByKey.set(approvalNodeKey(swarmId, Number(nodeId), callId), Date.now());
}

function clearImplicitApprovalResolved(swarmId: string, nodeId: number, callId: string) {
  if (!swarmId || !Number.isFinite(nodeId) || !callId) return;
  implicitResolvedApprovalByKey.delete(approvalNodeKey(swarmId, Number(nodeId), callId));
}

function isImplicitlyResolvedApproval(swarmId: string, nodeId: number, callId: string) {
  if (!swarmId || !Number.isFinite(nodeId) || !callId) return false;
  const ts = implicitResolvedApprovalByKey.get(approvalNodeKey(swarmId, Number(nodeId), callId));
  if (!Number.isFinite(ts)) return false;
  return Date.now() - Number(ts) <= IMPLICIT_RESOLVED_APPROVAL_TTL_MS;
}

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
type ApprovalLatencyStats = { count: number; total_ms: number; max_ms: number };
type ApprovalAttemptMeta = {
  job_id: string;
  call_id: string;
  node_id?: number;
  attempt: number;
  timeout_ms: number;
  sent_at_ms: number;
};
const approvalAttemptByRequestId = new Map<string, ApprovalAttemptMeta>();
const approvalMetrics = {
  submit_total: 0,
  attempt_total: 0,
  router_unavailable_total: 0,
  ack_resolved_total: 0,
  ack_started_total: 0,
  ack_rejected_total: 0,
  ack_timeout_total: 0,
  response_ok_total: 0,
  response_pending_total: 0,
  response_rejected_total: 0
};
const approvalLatencyByAckType: Record<ApprovalAckType, ApprovalLatencyStats> = {
  resolved: { count: 0, total_ms: 0, max_ms: 0 },
  started: { count: 0, total_ms: 0, max_ms: 0 },
  rejected: { count: 0, total_ms: 0, max_ms: 0 },
  timeout: { count: 0, total_ms: 0, max_ms: 0 }
};

function recordApprovalAckMetrics(
  requestId: string,
  ackType: ApprovalAckType,
  fallback: { job_id: string; call_id: string; node_id?: number; attempt: number; timeout_ms: number }
) {
  const meta = approvalAttemptByRequestId.get(requestId);
  const now = Date.now();
  const sentAt = Number(meta?.sent_at_ms || now);
  const latencyMs = Math.max(0, now - sentAt);
  const bucket = approvalLatencyByAckType[ackType];
  bucket.count += 1;
  bucket.total_ms += latencyMs;
  bucket.max_ms = Math.max(bucket.max_ms, latencyMs);

  if (ackType === 'resolved') approvalMetrics.ack_resolved_total += 1;
  if (ackType === 'started') approvalMetrics.ack_started_total += 1;
  if (ackType === 'rejected') approvalMetrics.ack_rejected_total += 1;
  if (ackType === 'timeout') approvalMetrics.ack_timeout_total += 1;

  logApprovalTrace('ack_metrics', {
    request_id: requestId,
    ack_type: ackType,
    latency_ms: latencyMs,
    timeout_ms: Number(meta?.timeout_ms ?? fallback.timeout_ms),
    attempt: Number(meta?.attempt ?? fallback.attempt),
    job_id: String(meta?.job_id ?? fallback.job_id),
    call_id: String(meta?.call_id ?? fallback.call_id),
    node_id: Number.isFinite(Number(meta?.node_id))
      ? Number(meta?.node_id)
      : Number.isFinite(Number(fallback.node_id))
      ? Number(fallback.node_id)
      : undefined,
    waiter_count: approvalAckWaiters.size,
    inflight_count: approvalAttemptByRequestId.size
  });
  approvalAttemptByRequestId.delete(requestId);
}

function approvalMetricsSnapshot() {
  const latency = Object.fromEntries(
    (Object.keys(approvalLatencyByAckType) as ApprovalAckType[]).map((key) => {
      const item = approvalLatencyByAckType[key];
      return [
        key,
        {
          count: item.count,
          avg_ms: item.count > 0 ? Math.round(item.total_ms / item.count) : 0,
          max_ms: item.max_ms
        }
      ];
    })
  );
  return {
    counters: { ...approvalMetrics },
    ack_waiters: approvalAckWaiters.size,
    inflight_attempts: approvalAttemptByRequestId.size,
    latency
  };
}

function normalizeNodeId(value: any): number | undefined {
  const n = Number(value);
  return Number.isFinite(n) ? n : undefined;
}

function compactApprovalLogValue(value: any, depth = 0): any {
  if (value == null) return value;
  if (typeof value === 'string') {
    return value.length > 240 ? `${value.slice(0, 240)}…` : value;
  }
  if (typeof value !== 'object') return value;
  if (depth >= 3) return '[truncated]';
  if (Array.isArray(value)) {
    const limit = 8;
    const out = value.slice(0, limit).map((item) => compactApprovalLogValue(item, depth + 1));
    if (value.length > limit) out.push(`[+${value.length - limit} more]`);
    return out;
  }
  const out: Record<string, any> = {};
  for (const [k, v] of Object.entries(value)) {
    out[k] = compactApprovalLogValue(v, depth + 1);
  }
  return out;
}

function logApprovalTrace(stage: string, payload: Record<string, any>) {
  try {
    console.log(
      '[backend APPROVAL]',
      JSON.stringify({
        stage,
        ts: new Date().toISOString(),
        ...compactApprovalLogValue(payload)
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

function mapRouterApprovalStatusToUi(status: any): ApprovalStatus | undefined {
  if (typeof status !== 'string') return undefined;
  if (status === 'approved_pending_ack' || status === 'denied_pending_ack') {
    return 'acknowledged';
  }
  if (status === 'pending') return 'pending';
  return undefined;
}

function approvalCommandChangeCount(command: any): number {
  if (!command || typeof command !== 'object') return 0;
  const changes = (command as any).changes;
  if (Array.isArray(changes)) return changes.length;
  if (changes && typeof changes === 'object') {
    const files = Array.isArray((changes as any).files) ? (changes as any).files.length : 0;
    const list = Array.isArray((changes as any).changes) ? (changes as any).changes.length : 0;
    return Math.max(files, list, Object.keys(changes).length);
  }
  return 0;
}

function approvalCommandLooksGeneric(command: any): boolean {
  if (typeof command !== 'string') return false;
  const normalized = command.trim().toLowerCase();
  return normalized === 'apply file changes' || normalized === 'review file changes';
}

function chooseApprovalCommand(previous: any, next: any) {
  if (next === undefined || next === null) return previous;
  if (previous === undefined || previous === null) return next;

  const previousChangeCount = approvalCommandChangeCount(previous);
  const nextChangeCount = approvalCommandChangeCount(next);

  if (nextChangeCount > previousChangeCount) return next;
  if (previousChangeCount > nextChangeCount) return previous;

  if (previousChangeCount > 0 && nextChangeCount > 0) {
    const nextDiffLen = JSON.stringify(next).length;
    const previousDiffLen = JSON.stringify(previous).length;
    return nextDiffLen >= previousDiffLen ? next : previous;
  }

  if (approvalCommandLooksGeneric(next) && !approvalCommandLooksGeneric(previous)) {
    return previous;
  }
  if (approvalCommandLooksGeneric(previous) && !approvalCommandLooksGeneric(next)) {
    return next;
  }

  return next;
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

function buildApprovalsSnapshotPayload() {
  return {
    approvals_version: latestApprovalsVersion,
    approvals: clonePendingApprovalsSnapshot()
  };
}

function replacePendingApprovalsFromRouterSnapshot(rawSnapshot: any, incomingVersion?: number) {
  if (Number.isFinite(incomingVersion)) {
    const normalized = Number(incomingVersion);
    if (normalized < latestApprovalsVersion) {
      return;
    }
    latestApprovalsVersion = normalized;
  } else {
    latestApprovalsVersion += 1;
  }

  const existingByKey = new Map<string, ApprovalRecord>();
  for (const [swarmId, byNode] of pendingApprovalsBySwarm.entries()) {
    for (const [nodeId, approvals] of byNode.entries()) {
      for (const approval of approvals ?? []) {
        if (!approval?.call_id) continue;
        existingByKey.set(`${swarmId}:${nodeId}:${approval.call_id}`, approval);
      }
    }
  }

  const nextBySwarm = new Map<string, Map<number, ApprovalRecord[]>>();
  const now = Date.now();
  pruneStaleWaitingNodes(now);
  pruneStaleImplicitResolvedApprovals(now);
  const snapshot = rawSnapshot && typeof rawSnapshot === 'object' ? rawSnapshot : {};

  for (const [swarmIdRaw, nodeMapRaw] of Object.entries(snapshot)) {
    if (!nodeMapRaw || typeof nodeMapRaw !== 'object') continue;
    const swarmId = String(swarmIdRaw);
    const byNode = new Map<number, ApprovalRecord[]>();

    for (const [nodeIdRaw, approvalsRaw] of Object.entries(nodeMapRaw as Record<string, any>)) {
      const nodeId = normalizeNodeId(nodeIdRaw);
      if (!Number.isFinite(nodeId)) continue;
      const parsedNodeId = Number(nodeId);
      if (!Array.isArray(approvalsRaw)) continue;

      const approvals: ApprovalRecord[] = [];
      for (const rawApproval of approvalsRaw) {
        const callId = String(rawApproval?.call_id || '').trim();
        if (!callId) continue;
        if (isImplicitlyResolvedApproval(swarmId, parsedNodeId, callId)) continue;
        const previous = existingByKey.get(`${swarmId}:${parsedNodeId}:${callId}`);
        const routerStatus = mapRouterApprovalStatusToUi(rawApproval?.approval_status);
        const preservedStatus = (
          previous?.status === 'submitted' ||
          previous?.status === 'acknowledged' ||
          previous?.status === 'started' ||
          previous?.status === 'resolved' ||
          previous?.status === 'rejected' ||
          previous?.status === 'timeout'
        )
          ? previous.status
          : (routerStatus ?? 'pending');
        const createdAtMs =
          typeof rawApproval?.created_at_ms === 'number'
            ? rawApproval.created_at_ms
            : typeof rawApproval?.created_at_ts === 'number'
            ? Math.floor(rawApproval.created_at_ts * 1000)
            : typeof previous?.created_at_ms === 'number'
            ? previous.created_at_ms
            : now;

        approvals.push({
          job_id: typeof rawApproval?.job_id === 'string' ? rawApproval.job_id : previous?.job_id,
          approval_id:
            typeof rawApproval?.approval_id === 'string'
              ? rawApproval.approval_id
              : previous?.approval_id,
          approval_status:
            typeof rawApproval?.approval_status === 'string'
              ? rawApproval.approval_status
              : previous?.approval_status,
          call_id: callId,
          injection_id:
            typeof rawApproval?.injection_id === 'string'
              ? rawApproval.injection_id
              : previous?.injection_id,
          command: chooseApprovalCommand(previous?.command, rawApproval?.command),
          reason: typeof rawApproval?.reason === 'string' ? rawApproval.reason : '',
          cwd: typeof rawApproval?.cwd === 'string' ? rawApproval.cwd : undefined,
          proposed_execpolicy_amendment: rawApproval?.proposed_execpolicy_amendment,
          available_decisions: rawApproval?.available_decisions,
          created_at_ms: createdAtMs,
          updated_at_ms: now,
          approval_seq: nextApprovalSeq(),
          status: preservedStatus,
          submit_attempts: previous?.submit_attempts,
          last_request_id: previous?.last_request_id
        });
      }

      if (approvals.length > 0) {
        approvals.sort((a, b) => a.created_at_ms - b.created_at_ms);
        byNode.set(parsedNodeId, approvals);
      }
    }

    if (byNode.size > 0) {
      nextBySwarm.set(swarmId, byNode);
    }
  }

  pendingApprovalsBySwarm.clear();
  for (const [swarmId, byNode] of nextBySwarm.entries()) {
    pendingApprovalsBySwarm.set(swarmId, byNode);
  }

  // Router snapshots can briefly arrive empty while agents are already flagged
  // waiting on approval. Preserve existing pending rows for those nodes until
  // we receive an explicit resolve/start/reject or waiting flag clears.
  for (const [swarmId, byNode] of pendingApprovalsBySwarm.entries()) {
    for (const nodeId of byNode.keys()) {
      const key = waitingNodeKey(swarmId, nodeId);
      if (!waitingOnApprovalByNode.has(key)) continue;
      waitingOnApprovalByNode.set(key, now);
    }
  }
  for (const [key, lastSeenMs] of waitingOnApprovalByNode.entries()) {
    if (now - lastSeenMs > WAITING_APPROVAL_STALE_MS) continue;
    const [swarmId, nodeIdRaw] = key.split(':');
    const nodeId = Number(nodeIdRaw);
    if (!swarmId || !Number.isFinite(nodeId)) continue;

    const byNode = pendingApprovalsBySwarm.get(swarmId);
    const hasCurrent = !!byNode && Array.isArray(byNode.get(nodeId)) && (byNode.get(nodeId)!.length > 0);
    if (hasCurrent) continue;

    const existingByNode = existingByKey;
    const prefix = `${swarmId}:${nodeId}:`;
    const carryForward: ApprovalRecord[] = [];
    for (const [approvalKeyToken, approval] of existingByNode.entries()) {
      if (!approvalKeyToken.startsWith(prefix)) continue;
      if (!approval) continue;
      if (isImplicitlyResolvedApproval(swarmId, nodeId, String(approval.call_id || ''))) continue;
      const status = approval.status;
      if (status !== 'pending' && status !== 'submitted' && status !== 'acknowledged') continue;
      carryForward.push({
        ...approval,
        updated_at_ms: now,
        approval_seq: nextApprovalSeq()
      });
    }
    if (carryForward.length === 0) continue;
    carryForward.sort((a, b) => Number(a.created_at_ms) - Number(b.created_at_ms));
    const mergedByNode = byNode ?? new Map<number, ApprovalRecord[]>();
    mergedByNode.set(nodeId, carryForward);
    pendingApprovalsBySwarm.set(swarmId, mergedByNode);
  }
}

function broadcastApprovalsSnapshot() {
  hub.broadcast({
    type: 'approvals_snapshot',
    payload: buildApprovalsSnapshotPayload()
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
  const existingStatus = idx >= 0 ? existing[idx]?.status : undefined;
  const routerStatus = mapRouterApprovalStatusToUi(approval?.approval_status);
  const nextStatus: ApprovalStatus =
    existingStatus === 'submitted' ||
    existingStatus === 'acknowledged' ||
    existingStatus === 'started' ||
    existingStatus === 'resolved' ||
    existingStatus === 'rejected' ||
    existingStatus === 'timeout'
      ? existingStatus
      : (routerStatus ?? 'pending');
  const nextApproval = {
    ...approval,
    command: chooseApprovalCommand(idx >= 0 ? existing[idx]?.command : undefined, approval?.command),
    status: nextStatus,
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
        return { swarmId, nodeId: currentNodeId, byNode, approvals, idx, approval: approvals[idx]! };
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
    call_id: found.approval.call_id,
    created_at_ms: Number(found.approval.created_at_ms ?? now),
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
    markImplicitApprovalResolved(swarmId, id, callId);
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
  for (const key of Array.from(waitingOnApprovalByNode.keys())) {
    if (key.startsWith(`${swarmId}:`)) {
      waitingOnApprovalByNode.delete(key);
    }
  }
  approvalKeyByRequestId.clear();
  for (const key of Array.from(implicitResolvedApprovalByKey.keys())) {
    if (key.startsWith(`${swarmId}:`)) {
      implicitResolvedApprovalByKey.delete(key);
    }
  }
}

function clearReplyRoutesForSwarm(swarmId: string) {
  if (!swarmId) return;
  for (const [requestId, route] of pendingReplyRoutesByRequestId.entries()) {
    if (route.sourceSwarmId === swarmId || route.targetSwarmId === swarmId) {
      pendingReplyRoutesByRequestId.delete(requestId);
    }
  }
  for (const [injectionId, route] of replyRoutesByInjectionId.entries()) {
    if (route.sourceSwarmId === swarmId || route.targetSwarmId === swarmId) {
      replyRoutesByInjectionId.delete(injectionId);
      processedReplyRoutes.delete(injectionId);
    }
  }
}

type AutoRouteDirective =
  | { targetAlias?: string; mode: 'idle'; prompt: string; replyToSender?: boolean }
  | { targetAlias?: string; mode: 'all'; prompt: string; replyToSender?: boolean }
  | { targetAlias?: string; mode: 'nodes'; prompt: string; nodes: number[]; replyToSender?: boolean };

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
  const startRegex = /^(?:\s*(?:[-*+]|\d+\.)\s+|\s*>\s+)?(\/(?:swarm\[[^\]\r\n]+\]\/(?:all|idle|first-idle|(?:agent|node)\[[^\]\r\n]+\])(?:\/reply)?|all|(?:agent|node)\[[^\]\r\n]+\]))(?:\s+|$)/gm;
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
    if (!current) continue;
    const next = commands[i + 1];
    const prompt = text
      .slice(current.bodyStart, next ? next.lineStart : text.length)
      .trim();
    if (!prompt) continue;

    const command = current.command;

    if (command.startsWith('/swarm[')) {
      const crossAllMatch = command.match(/^\/swarm\[(.+?)\]\/all(?:\/reply)?$/);
      if (crossAllMatch) {
        const targetAlias = (crossAllMatch[1] ?? '').trim();
        if (targetAlias) {
          directives.push({
            targetAlias,
            mode: 'all',
            prompt,
            replyToSender: command.endsWith('/reply')
          });
        }
        continue;
      }

      const crossIdleMatch = command.match(/^\/swarm\[(.+?)\]\/(idle|first-idle)(?:\/reply)?$/);
      if (crossIdleMatch) {
        const targetAlias = (crossIdleMatch[1] ?? '').trim();
        if (targetAlias) {
          directives.push({
            targetAlias,
            mode: 'idle',
            prompt,
            replyToSender: command.endsWith('/reply')
          });
        }
        continue;
      }

      const crossAgentMatch = command.match(/^\/swarm\[(.+?)\]\/(?:agent|node)\[(.+?)\](?:\/reply)?$/);
      if (crossAgentMatch) {
        const targetAlias = (crossAgentMatch[1] ?? '').trim();
        const expr = (crossAgentMatch[2] ?? '').trim();
        const nodes = parseNodeSpec(expr);
        if (targetAlias && nodes.length > 0) {
          directives.push({
            targetAlias,
            mode: 'nodes',
            prompt,
            nodes,
            replyToSender: command.endsWith('/reply')
          });
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

function registerReplyRouteRequest(
  requestId: string,
  sourceSwarm: any,
  targetSwarm: any,
  sourceNodeId: number | undefined,
  sourceInjectionId: string
) {
  if (!requestId || !sourceSwarm || !targetSwarm) return;
  if (!Number.isFinite(sourceNodeId)) return;
  pendingReplyRoutesByRequestId.set(requestId, {
    sourceSwarmId: String(sourceSwarm.swarm_id),
    sourceAlias: String(sourceSwarm.alias),
    sourceNodeId: Number(sourceNodeId),
    targetSwarmId: String(targetSwarm.swarm_id),
    targetAlias: String(targetSwarm.alias),
    sourceInjectionId
  });
}

function tryFinalizeReplyRouteOnInjectAck(data: any) {
  const requestId = typeof data?.request_id === 'string' ? data.request_id : '';
  if (!requestId) return;
  const pending = pendingReplyRoutesByRequestId.get(requestId);
  if (!pending) return;

  const injectionId = typeof data?.injection_id === 'string' ? data.injection_id : '';
  const targetNodeId = normalizeNodeId(data?.node_id);
  const swarmId = typeof data?.swarm_id === 'string' ? data.swarm_id : '';
  if (!injectionId || !Number.isFinite(targetNodeId) || !swarmId) return;
  if (swarmId !== pending.targetSwarmId) return;

  replyRoutesByInjectionId.set(injectionId, {
    ...pending,
    targetNodeId: Number(targetNodeId)
  });
}

function handleReplyRouteFromTaskComplete(data: any) {
  const injectionId = typeof data?.injection_id === 'string' ? data.injection_id : '';
  if (!injectionId || processedReplyRoutes.has(injectionId)) return;
  const route = replyRoutesByInjectionId.get(injectionId);
  if (!route) return;
  processedReplyRoutes.add(injectionId);

  const finalText = extractText(data?.last_agent_message).trim();
  if (!finalText) return;

  const returnPrompt =
    `[Reply from /swarm[${route.targetAlias}]/node[${route.targetNodeId}] ` +
    `for request ${route.sourceInjectionId}]\n${finalText}`;

  try {
    const request_id = sendRouterCommand('inject', {
      swarm_id: route.sourceSwarmId,
      nodes: [route.sourceNodeId],
      content: returnPrompt
    });
    requestSwarmMap[request_id] = route.sourceSwarmId;
    requestPromptMap.set(request_id, returnPrompt);
    hub.broadcast({
      type: 'auto_reply_submitted',
      payload: {
        request_id,
        source_swarm_id: route.sourceSwarmId,
        source_alias: route.sourceAlias,
        source_node_id: route.sourceNodeId,
        target_swarm_id: route.targetSwarmId,
        target_alias: route.targetAlias,
        target_node_id: route.targetNodeId,
        source_injection_id: route.sourceInjectionId,
        target_injection_id: injectionId
      }
    });
  } catch {
    hub.broadcast({
      type: 'auto_reply_ignored',
      payload: {
        source_swarm_id: route.sourceSwarmId,
        source_alias: route.sourceAlias,
        source_node_id: route.sourceNodeId,
        target_swarm_id: route.targetSwarmId,
        target_alias: route.targetAlias,
        target_node_id: route.targetNodeId,
        source_injection_id: route.sourceInjectionId,
        target_injection_id: injectionId,
        reason: 'router unavailable'
      }
    });
  }
}

function handleAutoRoutingFromFinalText(data: any, finalText: string) {
  const injectionId = typeof data?.injection_id === 'string' ? data.injection_id : '';
  if (!injectionId || processedAutoRoutes.has(injectionId)) return;

  const sourceSwarmId = typeof data?.swarm_id === 'string' ? data.swarm_id : '';
  if (!sourceSwarmId) return;

  const sourceSwarm = state.getById(sourceSwarmId);
  if (!sourceSwarm) return;
  const sourceNodeId = normalizeNodeId(data?.node_id);

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
      if (directive.replyToSender) {
        registerReplyRouteRequest(
          request_id,
          sourceSwarm,
          targetSwarm,
          sourceNodeId,
          injectionId
        );
      }
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
          injection_id: injectionId,
          reply_to_sender: Boolean(directive.replyToSender)
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
          mode: directive.mode,
          reply_to_sender: Boolean(directive.replyToSender)
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
    if (!router.isConnected()) {
      resolve([]);
      return;
    }
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

function requestApprovalsSnapshot(
  timeoutMs = 2500
): Promise<{ approvals_version: number; approvals: Record<string, Record<number, ApprovalRecord[]>> }> {
  return new Promise((resolve, reject) => {
    if (!router.isConnected()) {
      resolve(buildApprovalsSnapshotPayload());
      return;
    }
    let request_id = '';
    try {
      request_id = sendRouterCommand('approvals_list', {});
    } catch (err) {
      reject(err instanceof Error ? err : new Error('Router unavailable'));
      return;
    }
    const timer = setTimeout(() => {
      pendingApprovalsRequests.delete(request_id);
      reject(new Error('Timed out waiting for approvals_list'));
    }, timeoutMs);
    pendingApprovalsRequests.set(request_id, { resolve, reject, timer });
  });
}

function requestProjectResumePreview(
  payload: {
    project_id: string;
    worker_swarm_ids?: string[];
    retry_failed?: boolean;
    reverify_completed?: boolean;
  },
  timeoutMs = 7000
): Promise<any> {
  return new Promise((resolve, reject) => {
    if (!router.isConnected()) {
      reject(new Error('Router unavailable'));
      return;
    }
    let request_id = '';
    try {
      request_id = sendRouterCommand('project_resume_preview', payload);
    } catch (err) {
      reject(err instanceof Error ? err : new Error('Router unavailable'));
      return;
    }
    const timer = setTimeout(() => {
      pendingProjectResumePreviewRequests.delete(request_id);
      reject(new Error('Timed out waiting for project_resume_preview'));
    }, timeoutMs);
    pendingProjectResumePreviewRequests.set(request_id, { resolve, reject, timer });
  });
}

function stopApprovalsSyncLoop() {
  if (approvalsSyncTimer) {
    clearInterval(approvalsSyncTimer);
    approvalsSyncTimer = null;
  }
}

function startApprovalsSyncLoop() {
  stopApprovalsSyncLoop();
  const syncOnce = async () => {
    if (!router.isConnected() || approvalsSyncInFlight) return;
    approvalsSyncInFlight = true;
    try {
      await requestApprovalsSnapshot(2000);
    } catch {
      // Best-effort sync; keep the loop alive.
    } finally {
      approvalsSyncInFlight = false;
    }
  };
  void syncOnce();
  approvalsSyncTimer = setInterval(() => {
    void syncOnce();
  }, 1000);
}

setInterval(() => {
  logApprovalTrace('metrics_summary', approvalMetricsSnapshot());
}, 30000);

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
  clearTimeout(timer.soft);
  clearTimeout(timer.hard);
  pendingLaunchTimers.delete(requestId);
  abandonedLaunchRequests.delete(requestId);
}

function isSlowLaunchProvider(providerBackend?: string, providerId?: string) {
  const backend = String(providerBackend || '').toLowerCase();
  const id = String(providerId || '').toLowerCase();
  return backend === 'aws' || backend === 'slurm' || id.includes('aws') || id.includes('slurm');
}

function startPendingLaunchTimer(
  requestId: string,
  opts?: { provider?: string; providerBackend?: string; softTimeoutMs?: number; hardTimeoutMs?: number }
) {
  const providerId = typeof opts?.provider === 'string' ? opts.provider : '';
  const providerBackend = typeof opts?.providerBackend === 'string' ? opts.providerBackend : '';
  const slowProvider = isSlowLaunchProvider(providerBackend, providerId);
  const softTimeoutMs = Number.isFinite(opts?.softTimeoutMs as number)
    ? Number(opts?.softTimeoutMs)
    : (slowProvider ? 15 * 60 * 1000 : 120000);
  const hardTimeoutMs = Number.isFinite(opts?.hardTimeoutMs as number)
    ? Number(opts?.hardTimeoutMs)
    : (slowProvider ? 45 * 60 * 1000 : 8 * 60 * 1000);

  clearPendingLaunchTimer(requestId);
  const startedAt = Date.now();

  const softTimer = setTimeout(() => {
    if (!pendingLaunchTimers.has(requestId)) return;
    const elapsedSec = Math.max(1, Math.floor((Date.now() - startedAt) / 1000));
    hub.broadcast({
      type: 'swarm_launch_progress',
      payload: {
        request_id: requestId,
        provider: providerBackend || undefined,
        provider_id: providerId || undefined,
        stage: 'delayed',
        message: `Launch is still in progress after ${elapsedSec}s`,
        timestamp: Date.now() / 1000
      }
    });
  }, softTimeoutMs);

  const hardTimer = setTimeout(() => {
    if (!pendingLaunchTimers.has(requestId)) return;
    pendingLaunchTimers.delete(requestId);
    abandonedLaunchRequests.add(requestId);
    hub.broadcast({
      type: 'command_rejected',
      payload: {
        request_id: requestId,
        reason:
          `launch exceeded hard timeout after ${Math.floor(hardTimeoutMs / 1000)}s; ` +
          'if resources appear later they will be auto-terminated'
      }
    });
  }, hardTimeoutMs);

  pendingLaunchTimers.set(requestId, {
    soft: softTimer,
    hard: hardTimer,
    startedAt,
    softTimeoutMs,
    hardTimeoutMs
  });
}

function parsePositiveTimeoutSeconds(value: any): number | undefined {
  const n = Number(value);
  if (!Number.isFinite(n) || n <= 0) return undefined;
  return Math.floor(n);
}

// --- Router Event Handling ---
router.on('event', (msg: any) => {
  console.log('Router event received:', msg.event);
  const { event, data } = msg;

  if (event === 'thread_status') {
    const swarmId = typeof data?.swarm_id === 'string' ? data.swarm_id : '';
    const nodeId = Number(data?.node_id);
    const activeFlags = Array.isArray(data?.status?.activeFlags) ? data.status.activeFlags : [];
    const waiting = activeFlags.includes('waitingOnApproval');
    if (swarmId && Number.isFinite(nodeId)) {
      if (waiting) {
        markNodeWaitingOnApproval(swarmId, Number(nodeId));
        // Opportunistically refresh approval snapshot when agent enters
        // waitingOnApproval, so UI can surface pending requests promptly.
        try {
          sendRouterCommand('approvals_list', {});
        } catch {
          // best-effort only
        }
      } else {
        clearNodeWaitingOnApproval(swarmId, Number(nodeId));
      }
    }
  }

  if (event === 'exec_approval_required') {
    const swarmId = typeof data?.swarm_id === 'string' ? data.swarm_id : '';
    const nodeId = Number(data?.node_id);
    const jobId = typeof data?.job_id === 'string' ? data.job_id : '';
    const callId = typeof data?.call_id === 'string' ? data.call_id : '';
    if (swarmId && Number.isFinite(nodeId)) {
      if (callId) {
        clearImplicitApprovalResolved(swarmId, Number(nodeId), callId);
      }
      upsertPendingApproval(swarmId, nodeId, {
        job_id: jobId,
        approval_id: typeof data?.approval_id === 'string' ? data.approval_id : undefined,
        approval_status: typeof data?.approval_status === 'string' ? data.approval_status : undefined,
        call_id: callId,
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
      const swarmId = typeof data?.swarm_id === 'string' ? data.swarm_id : '';
      const nodeId = Number(data?.node_id);
      if (swarmId && Number.isFinite(nodeId)) {
        // If command execution has begun, any pending approval for this exact
        // call is effectively resolved even if runtime skips explicit resolved event.
        removePendingApprovalByCallId(swarmId, data.call_id, Number(nodeId));
        clearNodeWaitingOnApproval(swarmId, Number(nodeId));
        broadcastApprovalsSnapshot();
      }
    }
    // Do not eagerly clear pending approvals on command_started.
    // Some runtimes emit exec_command_begin before/alongside approval-required
    // for the same call_id, and removing here can make dialogs disappear until
    // another user action triggers fresh state.
  }

  if (event === 'command_completed') {
    const swarmId = typeof data?.swarm_id === 'string' ? data.swarm_id : '';
    const callId = typeof data?.call_id === 'string' ? data.call_id : '';
    const nodeId = Number(data?.node_id);
    if (swarmId && callId && Number.isFinite(nodeId)) {
      markImplicitApprovalResolved(swarmId, Number(nodeId), callId);
      removePendingApprovalByCallId(swarmId, callId, Number(nodeId));
      clearNodeWaitingOnApproval(swarmId, Number(nodeId));
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
    if (requestId && pendingApprovalsRequests.has(requestId)) {
      const pending = pendingApprovalsRequests.get(requestId)!;
      clearTimeout(pending.timer);
      pendingApprovalsRequests.delete(requestId);
      pending.reject(new Error(typeof data?.reason === 'string' ? data.reason : 'command rejected'));
    }
    if (requestId && pendingProjectResumePreviewRequests.has(requestId)) {
      const pending = pendingProjectResumePreviewRequests.get(requestId)!;
      clearTimeout(pending.timer);
      pendingProjectResumePreviewRequests.delete(requestId);
      pending.reject(new Error(typeof data?.reason === 'string' ? data.reason : 'command rejected'));
    }
    if (requestId && approvalKeyByRequestId.has(requestId)) {
      const key = approvalKeyByRequestId.get(requestId)!;
      approvalKeyByRequestId.delete(requestId);
      const [jobId, nodeIdRaw, callId] = key.split(':');
      if (jobId && callId) {
        const parsedNode = Number(nodeIdRaw);
        transitionApprovalStatus(
          { jobId, callId, nodeId: Number.isFinite(parsedNode) ? parsedNode : undefined },
          'rejected'
        );
      }
      broadcastApprovalsSnapshot();
    }
    if (requestId) {
      pendingReplyRoutesByRequestId.delete(requestId);
      resolveApprovalAckByRequestId(requestId, {
        type: 'rejected',
        reason: typeof data?.reason === 'string' ? data.reason : 'command rejected',
        request_id: requestId
      });
    }
  }

  if (event === 'inject_ack') {
    const mappedPrompt =
      typeof data?.request_id === 'string' ? requestPromptMap.get(data.request_id) : undefined;
    const prompt =
      mappedPrompt ??
      (typeof data?.prompt === 'string' && data.prompt.trim().length > 0 ? data.prompt : undefined);
    if (prompt && typeof data?.injection_id === 'string') {
      data.prompt = prompt;
      injectionPromptMap.set(data.injection_id, prompt);
    }
    tryFinalizeReplyRouteOnInjectAck(data);
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

    if (typeof request_id === 'string' && request_id && abandonedLaunchRequests.has(request_id)) {
      abandonedLaunchRequests.delete(request_id);
      // Best-effort safety net: if launch exceeded hard timeout, terminate as soon as
      // it materializes to prevent orphaned cost-bearing resources.
      try {
        const terminateRequestId = sendRouterCommand('swarm_terminate', { swarm_id });
        requestSwarmMap[terminateRequestId] = swarm_id;
        hub.broadcast({
          type: 'swarm_launch_progress',
          payload: {
            request_id,
            provider: provider || undefined,
            provider_id: provider_id || undefined,
            stage: 'cleanup',
            message: 'Launch exceeded hard timeout; auto-terminating resources',
            timestamp: Date.now() / 1000
          }
        });
      } catch {
        // Leave swarm visible; user can still terminate manually.
      }
    }

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

  if (event === 'project_resume_preview') {
    const requestId = typeof data?.request_id === 'string' ? data.request_id : '';
    if (requestId && pendingProjectResumePreviewRequests.has(requestId)) {
      const pending = pendingProjectResumePreviewRequests.get(requestId)!;
      clearTimeout(pending.timer);
      pendingProjectResumePreviewRequests.delete(requestId);
      pending.resolve(data?.preview ?? null);
    }
  }

  if (event === 'approvals_list') {
    const approvals = data?.approvals ?? {};
    const approvalsVersionRaw = data?.approvals_version;
    const approvalsVersion =
      Number.isFinite(Number(approvalsVersionRaw)) ? Number(approvalsVersionRaw) : undefined;
    replacePendingApprovalsFromRouterSnapshot(approvals, approvalsVersion);
    broadcastApprovalsSnapshot();
    const requestId = typeof data?.request_id === 'string' ? data.request_id : '';
    if (requestId && pendingApprovalsRequests.has(requestId)) {
      const pending = pendingApprovalsRequests.get(requestId)!;
      clearTimeout(pending.timer);
      pendingApprovalsRequests.delete(requestId);
      pending.resolve(buildApprovalsSnapshotPayload());
    }
    return;
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
    handleReplyRouteFromTaskComplete(data);
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

  if (event === 'project_list' || event === 'projects_updated') {
    projectsCache = data?.projects && typeof data.projects === 'object' ? data.projects : {};
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
          {
            provider: swarm.provider,
            provider_id: swarm.provider_id,
            status: typeof swarm.status === 'string' ? swarm.status : 'unknown',
            slurm_state: typeof swarm.slurm_state === 'string' ? swarm.slurm_state : undefined
          }
        );
      } else {
        state.updateProviderMeta(swarm_id, swarm.provider, swarm.provider_id);
        state.updateStatus(
          swarm_id,
          typeof swarm.status === 'string' ? swarm.status : undefined,
          typeof swarm.slurm_state === 'string' ? swarm.slurm_state : undefined
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
    clearPendingApprovalsForSwarm(data.swarm_id);
    clearReplyRoutesForSwarm(data.swarm_id);
    broadcastApprovalsSnapshot();
    state.remove(data.swarm_id);
    hub.broadcast({ type: 'swarm_removed', payload: data });
    return;
  }

  // Handle router-driven removal (TTL prune or other cleanup)
  if (event === 'swarm_removed') {
    clearPendingApprovalsForSwarm(data.swarm_id);
    clearReplyRoutesForSwarm(data.swarm_id);
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
    sendRouterCommand('project_list', {});
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
  const matchedProvider = launchProviders.find(
    (p) => p && typeof p.id === 'string' && p.id === normalizedProvider
  );
  const providerBackend =
    typeof matchedProvider?.backend === 'string'
      ? matchedProvider.backend
      : (typeof normalizedProvider === 'string' ? normalizedProvider : undefined);
  const softTimeoutSeconds =
    parsePositiveTimeoutSeconds(matchedProvider?.launch_soft_timeout_seconds) ??
    parsePositiveTimeoutSeconds(matchedProvider?.defaults?.launch_soft_timeout_seconds);
  const hardTimeoutSeconds =
    parsePositiveTimeoutSeconds(matchedProvider?.launch_hard_timeout_seconds) ??
    parsePositiveTimeoutSeconds(matchedProvider?.defaults?.launch_hard_timeout_seconds);
  startPendingLaunchTimer(request_id, {
    provider: normalizedProvider,
    providerBackend,
    softTimeoutMs: softTimeoutSeconds ? softTimeoutSeconds * 1000 : undefined,
    hardTimeoutMs: hardTimeoutSeconds ? hardTimeoutSeconds * 1000 : undefined
  });

  res.json({ request_id });
});

app.post('/inject/:alias', (req, res) => {
  const swarm = state.getByAlias(req.params.alias);
  if (!swarm) return res.status(404).json({ error: 'Unknown swarm' });

  const { prompt, nodes, target_alias, selector, reply_to_sender, source_node_id } = req.body;

  if (!prompt || typeof prompt !== 'string') {
    return res.status(400).json({ error: 'Invalid prompt' });
  }

  // Cross-swarm routing mode: frontend can request that prompt be delivered
  // to another swarm with selector semantics (e.g. first idle node).
  if (typeof target_alias === 'string' && target_alias.trim()) {
    const targetSwarm = state.getByAlias(target_alias);
    if (!targetSwarm) return res.status(404).json({ error: 'Unknown target swarm' });
    const shouldReplyToSender = Boolean(reply_to_sender);
    const sourceNodeId = normalizeNodeId(source_node_id);
    const canRegisterReply = shouldReplyToSender && Number.isFinite(sourceNodeId);

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
      if (canRegisterReply) {
        registerReplyRouteRequest(
          request_id,
          swarm,
          targetSwarm,
          sourceNodeId,
          request_id
        );
      }
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
    if (canRegisterReply) {
      registerReplyRouteRequest(
        request_id,
        swarm,
        targetSwarm,
        sourceNodeId,
        request_id
      );
    }
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
  const force = Boolean(req.body?.force);
  let request_id = '';
  try {
    const terminateParams: Record<string, any> = {};
    if (downloadWorkspaces) terminateParams.download_workspaces_on_shutdown = true;
    if (force) terminateParams.force = true;
    request_id = sendRouterCommand('swarm_terminate', {
      swarm_id: swarm.swarm_id,
      terminate_params: Object.keys(terminateParams).length > 0 ? terminateParams : undefined
    });
  } catch (err) {
    return res.status(503).json({ error: 'Router unavailable. Try again in a moment.' });
  }
  requestSwarmMap[request_id] = swarm.swarm_id;

  res.json({ request_id });
});

app.post('/swarms/:swarmId/terminate', (req, res) => {
  const swarmId = String(req.params.swarmId || '').trim();
  if (!swarmId) return res.status(400).json({ error: 'Missing swarm id' });
  const downloadWorkspaces = Boolean(req.body?.download_workspaces_on_shutdown);
  const force = Boolean(req.body?.force);
  let request_id = '';
  try {
    const terminateParams: Record<string, any> = {};
    if (downloadWorkspaces) terminateParams.download_workspaces_on_shutdown = true;
    if (force) terminateParams.force = true;
    request_id = sendRouterCommand('swarm_terminate', {
      swarm_id: swarmId,
      terminate_params: Object.keys(terminateParams).length > 0 ? terminateParams : undefined
    });
  } catch {
    return res.status(503).json({ error: 'Router unavailable. Try again in a moment.' });
  }
  requestSwarmMap[request_id] = swarmId;
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

app.get('/projects', (req, res) => {
  res.json(projectsCache);
});

app.get('/approvals', (req, res) => {
  // Serve the backend cache; a background router sync loop keeps this current.
  return res.json(buildApprovalsSnapshotPayload());
});

app.get('/debug/approval-metrics', (req, res) => {
  return res.json(approvalMetricsSnapshot());
});

app.get('/providers', async (req, res) => {
  if (!router.isConnected()) {
    if (launchProviders.length > 0) {
      return res.json(launchProviders);
    }
    return res.json(fallbackProvidersCatalog());
  }
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
  approvalMetrics.submit_total += 1;

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

  logApprovalTrace('ui_submit_received', {
    job_id: String(job_id),
    call_id: String(call_id),
    node_id: criteriaNodeId,
    injection_id: typeof injection_id === 'string' ? injection_id : undefined,
    approved: normalizedApproved,
    decision: decision ?? null
  });
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
    approvalMetrics.router_unavailable_total += 1;
    return res.status(503).json({ error: 'Router unavailable. Try again in a moment.' });
  }

  const attempt = 1;
  // Native approval acks on local and remote workers are expected quickly.
  // Keep the submit state short so unsupported/uncorrelated approvals do not
  // linger in the UI for several seconds before timing out.
  const timeoutMs = 1500;
  approvalMetrics.attempt_total += 1;
  approvalMetrics.response_ok_total += 1;
  approvalAttemptByRequestId.set(request_id, {
    job_id: String(job_id),
    call_id: String(call_id),
    node_id: criteria.node_id,
    attempt,
    timeout_ms: timeoutMs,
    sent_at_ms: Date.now()
  });
  logApprovalTrace('router_approve_execution_sent', {
    request_id,
    attempt,
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
    { submit_attempts: attempt, last_request_id: request_id }
  );
  broadcastApprovalsSnapshot();

  // Track downstream ack asynchronously to keep approval submit latency low.
  void (async () => {
    const ack = await waitForApprovalAck(request_id, criteria, timeoutMs);
    recordApprovalAckMetrics(request_id, ack.type, {
      job_id: String(job_id),
      call_id: String(call_id),
      node_id: criteria.node_id,
      attempt,
      timeout_ms: timeoutMs
    });
    logApprovalTrace('router_approve_execution_ack', {
      request_id,
      attempt,
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
      return;
    }

    if (ack.type === 'rejected') {
      approvalMetrics.response_rejected_total += 1;
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
      return;
    }

    // A short local ack timeout only means we did not observe an explicit
    // downstream ack quickly. Router remains authoritative for approval
    // lifecycle, so do not hide the approval from the UI here. Keep the last
    // submitted/acknowledged state visible until router resolves or removes it.
    approvalMetrics.response_pending_total += 1;
    logApprovalTrace('router_approve_execution_ack_deferred', {
      request_id,
      attempt,
      job_id: String(job_id),
      call_id: String(call_id),
      node_id: criteria.node_id,
      note: 'keeping approval visible pending router resolution'
    });
  })();

  return res.json({
    request_id,
    status: 'submitted',
    attempts: attempt
  });
});

app.post('/projects', (req, res) => {
  if (!router.isConnected()) {
    return res.status(503).json({ error: 'Router unavailable. Try again in a moment.' });
  }
  const {
    title,
    repo_path,
    repo_mode,
    github_owner,
    github_repo,
    github_create_if_missing,
    github_visibility,
    base_branch,
    worker_swarm_ids,
    tasks,
    workspace_subdir,
    auto_start
  } = req.body || {};
  const hasRepoSpec =
    (typeof repo_path === 'string' && repo_path.trim().length > 0) ||
    (typeof github_owner === 'string' && github_owner.trim().length > 0 && typeof github_repo === 'string' && github_repo.trim().length > 0);
  if (!title || !hasRepoSpec || !Array.isArray(worker_swarm_ids) || !Array.isArray(tasks)) {
    return res.status(400).json({ error: 'title, a repository path or GitHub org/repo, worker_swarm_ids, and tasks are required' });
  }
  try {
    const request_id = sendRouterCommand('project_create', {
      title,
      repo_path,
      repo_mode,
      github_owner,
      github_repo,
      github_create_if_missing,
      github_visibility,
      base_branch,
      worker_swarm_ids,
      tasks,
      workspace_subdir,
      auto_start
    });
    return res.json({ request_id, status: 'submitted' });
  } catch {
    return res.status(503).json({ error: 'Router unavailable. Try again in a moment.' });
  }
});

app.post('/projects/plan', (req, res) => {
  if (!router.isConnected()) {
    return res.status(503).json({ error: 'Router unavailable. Try again in a moment.' });
  }
  const {
    title,
    repo_path,
    repo_mode,
    github_owner,
    github_repo,
    github_create_if_missing,
    github_visibility,
    spec,
    planner_swarm_id,
    worker_swarm_ids,
    base_branch,
    workspace_subdir,
    auto_start
  } = req.body || {};
  const hasRepoSpec =
    (typeof repo_path === 'string' && repo_path.trim().length > 0) ||
    (typeof github_owner === 'string' && github_owner.trim().length > 0 && typeof github_repo === 'string' && github_repo.trim().length > 0);
  if (!title || !hasRepoSpec || !spec || !planner_swarm_id || !Array.isArray(worker_swarm_ids)) {
    return res.status(400).json({
      error: 'title, a repository path or GitHub org/repo, spec, planner_swarm_id, and worker_swarm_ids are required'
    });
  }
  try {
    const request_id = sendRouterCommand('project_plan', {
      title,
      repo_path,
      repo_mode,
      github_owner,
      github_repo,
      github_create_if_missing,
      github_visibility,
      spec,
      planner_swarm_id,
      worker_swarm_ids,
      base_branch,
      workspace_subdir,
      auto_start
    });
    return res.json({ request_id, status: 'submitted' });
  } catch {
    return res.status(503).json({ error: 'Router unavailable. Try again in a moment.' });
  }
});

app.post('/projects/:projectId/start', (req, res) => {
  if (!router.isConnected()) {
    return res.status(503).json({ error: 'Router unavailable. Try again in a moment.' });
  }
  try {
    const request_id = sendRouterCommand('project_start', {
      project_id: req.params.projectId
    });
    return res.json({ request_id, status: 'submitted' });
  } catch {
    return res.status(503).json({ error: 'Router unavailable. Try again in a moment.' });
  }
});

app.post('/projects/:projectId/resume', (req, res) => {
  if (!router.isConnected()) {
    return res.status(503).json({ error: 'Router unavailable. Try again in a moment.' });
  }
  const { worker_swarm_ids, retry_failed, reverify_completed } = req.body || {};
  try {
    const request_id = sendRouterCommand('project_resume', {
      project_id: req.params.projectId,
      worker_swarm_ids,
      retry_failed: Boolean(retry_failed),
      reverify_completed: reverify_completed === undefined ? true : Boolean(reverify_completed)
    });
    return res.json({ request_id, status: 'submitted' });
  } catch {
    return res.status(503).json({ error: 'Router unavailable. Try again in a moment.' });
  }
});

app.post('/projects/:projectId/resume-preview', async (req, res) => {
  if (!router.isConnected()) {
    return res.status(503).json({ error: 'Router unavailable. Try again in a moment.' });
  }
  const { worker_swarm_ids, retry_failed, reverify_completed } = req.body || {};
  try {
    const preview = await requestProjectResumePreview({
      project_id: req.params.projectId,
      worker_swarm_ids,
      retry_failed: Boolean(retry_failed),
      reverify_completed: reverify_completed === undefined ? true : Boolean(reverify_completed)
    });
    return res.json(preview ?? {});
  } catch (err) {
    return res.status(400).json({ error: err instanceof Error ? err.message : 'Failed to build resume preview.' });
  }
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
      sendRouterCommand('project_list', {});
      sendRouterCommand('approvals_list', {});
      startApprovalsSyncLoop();

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
  stopApprovalsSyncLoop();
  connectWithRetry().catch(() => {});
});

(async () => {
  await connectWithRetry();
  const PORT = Number(process.env.CODESWARM_WEB_BACKEND_PORT || process.env.PORT || 4000);
  server.listen(PORT, () => {
    console.log(`Backend running on http://localhost:${PORT}`);
  });
})();
