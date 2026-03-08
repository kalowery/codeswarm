import { create } from 'zustand'
import { persist } from 'zustand/middleware'

const MAX_TURNS_PER_NODE = 300
const STREAM_IDLE_COMPLETE_MS = 1500
const RESOLVED_APPROVAL_TTL_MS = 120000
const PENDING_APPROVAL_STICKY_MS = 10000
const RESOLVED_APPROVAL_STICKY_MS = 5000

export type TurnPhase =
  | 'idle'
  | 'streaming'
  | 'awaiting_approval'
  | 'executing'
  | 'completed'
  | 'error'

export interface ExecutionState {
  call_id: string
  command: string[] | string
  cwd?: string
  stdout?: string
  stderr?: string
  exit_code?: number
  started_at?: number
  completed_at?: number
  status: 'running' | 'completed'
}

export interface TokenUsage {
  total_tokens: number
  input_tokens?: number
  cached_input_tokens?: number
  output_tokens?: number
  reasoning_output_tokens?: number
  last_total_tokens?: number
  last_input_tokens?: number
  last_cached_input_tokens?: number
  last_output_tokens?: number
  last_reasoning_output_tokens?: number
  model_context_window?: number
  usage_source?: string
}

export interface NodeTurn {
  injection_id: string
  prompt: string
  deltas: string[]
  reasoning: string
  phase: TurnPhase
  has_final_answer?: boolean
  execution?: ExecutionState
  approval?: {
    call_id: string
    command: string[] | string
    reason: string
    cwd?: string
    proposed_execpolicy_amendment?: string[]
    available_decisions?: Array<string | Record<string, any>>
  }
  error?: string
  usage?: TokenUsage
}

export interface PendingApproval {
  call_id: string
  injection_id?: string
  created_at_ms?: number
  updated_at_ms?: number
  approval_seq?: number
  status?: 'pending' | 'submitted' | 'acknowledged' | 'started' | 'resolved' | 'rejected' | 'timeout'
  submit_attempts?: number
  last_request_id?: string
  command: string[] | string
  reason: string
  cwd?: string
  proposed_execpolicy_amendment?: string[]
  available_decisions?: Array<string | Record<string, any>>
}

export interface NodeState {
  node_id: number
  turns: NodeTurn[]
}

const capTurns = (turns: NodeTurn[]) =>
  turns.length > MAX_TURNS_PER_NODE
    ? turns.slice(-MAX_TURNS_PER_NODE)
    : turns

export interface SwarmRecord {
  swarm_id: string
  alias: string
  job_id: string
  node_count: number
  status: string
  slurm_state?: string
  provider?: string
  provider_id?: string
  known_exec_policies?: string[][]
  pending_approvals?: Record<number, PendingApproval[]>
  nodes: Record<number, NodeState>
}

export interface InterSwarmQueueItem {
  queue_id: string
  request_id?: string
  source_swarm_id?: string
  target_swarm_id: string
  selector?: string
  content?: string
  created_at?: number
}

export interface PendingLaunchRecord {
  alias: string
  stage?: string
  message?: string
  provider?: string
  provider_id?: string
  created_at: number
  updated_at: number
}

interface SwarmStore {
  swarms: Record<string, SwarmRecord>
  interSwarmQueue: InterSwarmQueueItem[]
  pendingLaunches: Record<string, PendingLaunchRecord>
  selectedSwarm?: string
  pendingPrompt?: string
  launchError: string | null
  activeNodeBySwarm: Record<string, number>
  setActiveNode: (swarm_id: string, node_id: number) => void
  setPendingPrompt: (prompt: string) => void
  setLaunchError: (message: string) => void
  clearLaunchError: () => void
  addPendingLaunch: (request_id: string, alias: string) => void
  updatePendingLaunch: (request_id: string, update: Partial<PendingLaunchRecord>) => void
  removePendingLaunch: (request_id: string) => void
  setSwarms: (swarms: any[]) => void
  setInterSwarmQueue: (items: InterSwarmQueueItem[]) => void
  addOrUpdateSwarm: (swarm: SwarmRecord) => void
  removeSwarm: (swarm_id: string) => void
  selectSwarm: (swarm_id: string) => void
  handleMessage: (msg: any) => void
}

export const useSwarmStore = create<SwarmStore>()(persist((set, get) => {
  // Track completions that arrive before turn_started
  const pendingComplete: Record<string, boolean> = {} // deprecated
  const pendingReasoningDeltas: Record<string, string[]> = {}
  const pendingReasoning: Record<string, string> = {}
  const pendingTaskComplete: Record<string, { content?: string }> = {}
  const idleCompletionTimers: Record<string, ReturnType<typeof setTimeout>> = {}
  let latestApprovalsSnapshot: Record<string, any> = {}
  const resolvedApprovalsUntil: Record<string, number> = {}

  const approvalResolutionKey = (swarmId: string, nodeId: number, callId: string) =>
    `${swarmId}:${nodeId}:${callId}`

  const pruneResolvedApprovals = () => {
    const now = Date.now()
    for (const [key, expiresAt] of Object.entries(resolvedApprovalsUntil)) {
      if (expiresAt <= now) {
        delete resolvedApprovalsUntil[key]
      }
    }
  }

  const markApprovalResolved = (swarmId: string, nodeId: number, callId: string) => {
    if (!swarmId || !Number.isFinite(nodeId) || !callId) return
    pruneResolvedApprovals()
    resolvedApprovalsUntil[approvalResolutionKey(swarmId, nodeId, callId)] =
      Date.now() + RESOLVED_APPROVAL_TTL_MS
  }

  const isApprovalRecentlyResolved = (swarmId: string, nodeId: number, callId: string) => {
    if (!swarmId || !Number.isFinite(nodeId) || !callId) return false
    pruneResolvedApprovals()
    const key = approvalResolutionKey(swarmId, nodeId, callId)
    const expiresAt = resolvedApprovalsUntil[key]
    return typeof expiresAt === 'number' && expiresAt > Date.now()
  }

  const shouldKeepApprovalFromCache = (approval: PendingApproval) => {
    const updatedAt = typeof approval.updated_at_ms === 'number' ? approval.updated_at_ms : 0
    const ageMs = Date.now() - updatedAt
    const status = approval.status
    if (status === 'pending' || status === 'submitted' || status === 'acknowledged') {
      return ageMs <= PENDING_APPROVAL_STICKY_MS
    }
    if (status === 'started' || status === 'resolved' || status === 'rejected' || status === 'timeout') {
      return ageMs <= RESOLVED_APPROVAL_STICKY_MS
    }
    return ageMs <= PENDING_APPROVAL_STICKY_MS
  }

  const mergeSnapshotWithStickyPending = (
    swarmId: string,
    snapshotPending: Record<number, PendingApproval[]>,
    existingPending: Record<number, PendingApproval[]> | undefined
  ): Record<number, PendingApproval[]> => {
    const merged: Record<number, PendingApproval[]> = {}
    const existing = existingPending ?? {}

    const upsert = (nodeId: number, approval: PendingApproval) => {
      const current = merged[nodeId] ?? []
      const prior = current.find((a) => a.call_id === approval.call_id)
      if (!prior || isIncomingApprovalNewer(prior, approval)) {
        merged[nodeId] = [...current.filter((a) => a.call_id !== approval.call_id), approval]
      }
    }

    for (const [nodeIdRaw, approvals] of Object.entries(snapshotPending)) {
      const nodeId = Number(nodeIdRaw)
      if (!Number.isFinite(nodeId)) continue
      for (const approval of approvals ?? []) {
        upsert(nodeId, approval)
      }
    }

    for (const [nodeIdRaw, approvals] of Object.entries(existing)) {
      const nodeId = Number(nodeIdRaw)
      if (!Number.isFinite(nodeId)) continue
      for (const approval of approvals ?? []) {
        if (!approval || !approval.call_id) continue
        if (isApprovalRecentlyResolved(swarmId, nodeId, approval.call_id)) continue
        if (!shouldKeepApprovalFromCache(approval)) continue
        upsert(nodeId, approval)
      }
    }

    return merged
  }

  const markPendingApprovalStatus = (
    pending: Record<number, PendingApproval[]> | undefined,
    callId: string | undefined,
    status: PendingApproval['status'],
    nodeId?: number
  ): Record<number, PendingApproval[]> => {
    if (!pending || !callId) return pending ?? {}
    const next: Record<number, PendingApproval[]> = {}
    let changed = false
    const now = Date.now()
    for (const [nodeKey, approvals] of Object.entries(pending)) {
      const currentNodeId = Number(nodeKey)
      const source = approvals ?? []
      if (Number.isFinite(nodeId) && currentNodeId !== nodeId) {
        if (source.length > 0) next[currentNodeId] = source
        continue
      }
      const updated = source.map((approval) => {
        if (approval.call_id !== callId) return approval
        changed = true
        return {
          ...approval,
          status,
          updated_at_ms: now
        }
      })
      if (updated.length > 0) next[currentNodeId] = updated
    }
    return changed ? next : (pending ?? {})
  }

  const parsePendingSnapshotForSwarm = (swarmId: string): Record<number, PendingApproval[]> => {
    const rawByNode = latestApprovalsSnapshot[swarmId]
    const nextPending: Record<number, PendingApproval[]> = {}
    if (!rawByNode || typeof rawByNode !== 'object') return nextPending
    for (const [nodeIdRaw, list] of Object.entries(rawByNode)) {
      const nodeId = Number(nodeIdRaw)
      if (!Number.isFinite(nodeId)) continue
      if (!Array.isArray(list)) continue
      const normalized = list
        .filter((a) => a && typeof a.call_id === 'string' && a.call_id.length > 0)
        .map((a) => ({
          call_id: a.call_id,
          injection_id: typeof a.injection_id === 'string' ? a.injection_id : undefined,
          created_at_ms: typeof a.created_at_ms === 'number' ? a.created_at_ms : undefined,
          updated_at_ms: typeof a.updated_at_ms === 'number' ? a.updated_at_ms : undefined,
          approval_seq: typeof a.approval_seq === 'number' ? a.approval_seq : undefined,
          status: typeof a.status === 'string' ? a.status : undefined,
          submit_attempts: typeof a.submit_attempts === 'number' ? a.submit_attempts : undefined,
          last_request_id: typeof a.last_request_id === 'string' ? a.last_request_id : undefined,
          command: a.command,
          reason: typeof a.reason === 'string' ? a.reason : '',
          cwd: typeof a.cwd === 'string' ? a.cwd : undefined,
          proposed_execpolicy_amendment: Array.isArray(a.proposed_execpolicy_amendment)
            ? a.proposed_execpolicy_amendment
            : undefined,
          available_decisions: Array.isArray(a.available_decisions)
            ? a.available_decisions
            : undefined
        })) as PendingApproval[]
      const active = normalized.filter(
        (a) => !isApprovalRecentlyResolved(swarmId, nodeId, a.call_id)
      )
      if (active.length > 0) nextPending[nodeId] = active
    }
    return nextPending
  }

  const timerKey = (swarmId: string, nodeId: number, injectionId: string) =>
    `${swarmId}:${nodeId}:${injectionId}`

  const clearIdleCompletionTimer = (swarmId: string, nodeId: number, injectionId: string) => {
    const key = timerKey(String(swarmId), Number(nodeId), String(injectionId))
    const timer = idleCompletionTimers[key]
    if (timer) {
      clearTimeout(timer)
      delete idleCompletionTimers[key]
    }
  }

  const scheduleIdleCompletion = (swarmId: string, nodeId: number, injectionId: string) => {
    clearIdleCompletionTimer(swarmId, nodeId, injectionId)
    const key = timerKey(String(swarmId), Number(nodeId), String(injectionId))
    idleCompletionTimers[key] = setTimeout(() => {
      const swarm = get().swarms[String(swarmId)]
      if (!swarm) return
      const node = swarm.nodes[Number(nodeId)]
      if (!node) return

      let changed = false
      const updatedTurns: NodeTurn[] = node.turns.map((t) => {
        if (t.injection_id !== injectionId) return t
        if (t.phase === 'streaming') {
          changed = true
          return ({ ...t, phase: 'completed' } as NodeTurn)
        }
        return t
      })

      delete idleCompletionTimers[key]

      if (!changed) return
      get().addOrUpdateSwarm({
        ...swarm,
        nodes: {
          ...swarm.nodes,
          [Number(nodeId)]: { ...node, turns: capTurns(updatedTurns) }
        }
      })
    }, STREAM_IDLE_COMPLETE_MS)
  }
  const extractExecPolicyAmendment = (decision: unknown): string[] | undefined => {
    if (!decision || typeof decision !== 'object') return undefined
    const d = decision as Record<string, any>
    const approvedAmendment = d.approved_execpolicy_amendment
    if (
      approvedAmendment &&
      typeof approvedAmendment === 'object' &&
      Array.isArray(approvedAmendment.proposed_execpolicy_amendment)
    ) {
      return approvedAmendment.proposed_execpolicy_amendment
    }
    const acceptAmendment = d.acceptWithExecpolicyAmendment
    if (
      acceptAmendment &&
      typeof acceptAmendment === 'object' &&
      Array.isArray(acceptAmendment.execpolicy_amendment)
    ) {
      return acceptAmendment.execpolicy_amendment
    }
    return undefined
  }

  const removePendingApprovalByCallId = (
    pending: Record<number, PendingApproval[]> | undefined,
    callId: string | undefined,
    nodeId?: number
  ): Record<number, PendingApproval[]> => {
    if (!pending || !callId) return pending ?? {}
    const next: Record<number, PendingApproval[]> = {}
    for (const [nodeKey, approvals] of Object.entries(pending)) {
      const currentNodeId = Number(nodeKey)
      if (Number.isFinite(nodeId) && currentNodeId !== nodeId) {
        if ((approvals ?? []).length > 0) next[currentNodeId] = approvals ?? []
        continue
      }
      const filtered = (approvals ?? []).filter((a) => a.call_id !== callId)
      if (filtered.length > 0) next[currentNodeId] = filtered
    }
    return next
  }

  const removePendingApprovalByInjectionId = (
    pending: Record<number, PendingApproval[]> | undefined,
    injectionId: string | undefined,
    nodeId?: number
  ): Record<number, PendingApproval[]> => {
    if (!pending || !injectionId) return pending ?? {}
    const next: Record<number, PendingApproval[]> = {}
    for (const [nodeKey, approvals] of Object.entries(pending)) {
      const currentNodeId = Number(nodeKey)
      if (Number.isFinite(nodeId) && currentNodeId !== nodeId) {
        if ((approvals ?? []).length > 0) next[currentNodeId] = approvals ?? []
        continue
      }
      const filtered = (approvals ?? []).filter((a) => a.injection_id !== injectionId)
      if (filtered.length > 0) next[currentNodeId] = filtered
    }
    return next
  }

  const approvalFreshness = (approval: PendingApproval) => {
    if (typeof approval.approval_seq === 'number') return approval.approval_seq
    if (typeof approval.updated_at_ms === 'number') return approval.updated_at_ms
    if (typeof approval.created_at_ms === 'number') return approval.created_at_ms
    return 0
  }

  const isIncomingApprovalNewer = (existing: PendingApproval | undefined, incoming: PendingApproval) => {
    if (!existing) return true
    const existingSeq = existing.approval_seq
    const incomingSeq = incoming.approval_seq
    if (typeof existingSeq === 'number' || typeof incomingSeq === 'number') {
      if (typeof incomingSeq !== 'number') return false
      if (typeof existingSeq !== 'number') return true
      return incomingSeq >= existingSeq
    }
    return approvalFreshness(incoming) >= approvalFreshness(existing)
  }

  return {
    swarms: {},
    interSwarmQueue: [],
    pendingLaunches: {},
    selectedSwarm: undefined,
    pendingPrompt: undefined,
    launchError: null,
    activeNodeBySwarm: {},
    setActiveNode: (swarm_id, node_id) =>
      set((state) => ({
        activeNodeBySwarm: {
          ...state.activeNodeBySwarm,
          [swarm_id]: node_id
        }
      })),
    setPendingPrompt: (prompt: string) => set({ pendingPrompt: prompt }),
    setInterSwarmQueue: (items: InterSwarmQueueItem[]) =>
      set({ interSwarmQueue: Array.isArray(items) ? items : [] }),
    setLaunchError: (message: string) => set({ launchError: message }),
    clearLaunchError: () => set({ launchError: null }),
    addPendingLaunch: (request_id: string, alias: string) =>
      set((state) => ({
        // Launch progress events can arrive before this local placeholder exists;
        // preserve any existing progress metadata when creating/updating the ghost card.
        pendingLaunches: {
          ...state.pendingLaunches,
          [request_id]: {
            alias,
            stage: state.pendingLaunches[request_id]?.stage,
            message: state.pendingLaunches[request_id]?.message,
            provider: state.pendingLaunches[request_id]?.provider,
            provider_id: state.pendingLaunches[request_id]?.provider_id,
            created_at: state.pendingLaunches[request_id]?.created_at ?? Date.now(),
            updated_at: Date.now()
          }
        }
      })),
    updatePendingLaunch: (request_id: string, update: Partial<PendingLaunchRecord>) =>
      set((state) => {
        const existing = state.pendingLaunches[request_id]
        if (!existing) {
          return {
            pendingLaunches: {
              ...state.pendingLaunches,
              [request_id]: {
                alias: 'Launching swarm',
                created_at: Date.now(),
                updated_at: Date.now(),
                ...update
              }
            }
          }
        }
        return {
          pendingLaunches: {
            ...state.pendingLaunches,
            [request_id]: {
              ...existing,
              ...update,
              updated_at: Date.now()
            }
          }
        }
      }),
    removePendingLaunch: (request_id: string) =>
      set((state) => {
        const copy = { ...state.pendingLaunches }
        delete copy[request_id]
        return { pendingLaunches: copy }
      }),

    setSwarms: (swarms) => {
      set((state) => {
        const updated: Record<string, SwarmRecord> = {}

        swarms.forEach((s) => {
          const existing = state.swarms[s.swarm_id]
          const snapshotPending = parsePendingSnapshotForSwarm(s.swarm_id)
          const hasSnapshotPending = Object.keys(snapshotPending).length > 0

          if (existing) {
            // Preserve existing nodes and turns, update metadata
            updated[s.swarm_id] = {
              ...existing,
              ...s,
              known_exec_policies:
                s.known_exec_policies ?? existing.known_exec_policies ?? [],
              pending_approvals:
                hasSnapshotPending
                  ? snapshotPending
                  : s.pending_approvals ?? existing.pending_approvals ?? {},
              nodes: existing.nodes
            }
          } else {
            // Initialize fresh swarm
            const nodes: Record<number, NodeState> = {}
            for (let i = 0; i < s.node_count; i++) {
              nodes[i] = { node_id: i, turns: [] }
            }

            updated[s.swarm_id] = {
              ...s,
              known_exec_policies: [],
              pending_approvals:
                hasSnapshotPending
                  ? snapshotPending
                  : s.pending_approvals ?? {},
              nodes
            }
          }
        })

        return { swarms: updated }
      })
    },

    addOrUpdateSwarm: (swarm) => {
      set((state) => {
        const existing = state.swarms[swarm.swarm_id]

        // Respect provided nodes if present (for immutable updates)
        let nodes = swarm.nodes

        if (!nodes) {
          nodes = existing?.nodes
        }

        // Initialize nodes if still missing
        if (!nodes) {
          nodes = {}
          for (let i = 0; i < swarm.node_count; i++) {
            nodes[i] = { node_id: i, turns: [] }
          }
        }

        return {
          swarms: {
            ...state.swarms,
            [swarm.swarm_id]: {
              ...existing,
              ...swarm,
              known_exec_policies:
                swarm.known_exec_policies ?? existing?.known_exec_policies ?? [],
              pending_approvals:
                swarm.pending_approvals ?? existing?.pending_approvals ?? {},
              nodes
            }
          }
        }
      })
    },

    removeSwarm: (swarm_id) => {
      set((state) => {
        const copy = { ...state.swarms }
        delete copy[swarm_id]
        return { swarms: copy }
      })
    },

    selectSwarm: (swarm_id) => set({ selectedSwarm: swarm_id }),

    handleMessage: (msg) => {
      const { type, payload } = msg

      if (type === 'swarm_added') {
        get().addOrUpdateSwarm(payload)
      }

      if (type === 'swarm_launched') {
        // Remove pending launch ghost when real swarm is confirmed
        if (payload.request_id) {
          get().removePendingLaunch(payload.request_id)
        }
        // Clear any previous launch error
        get().clearLaunchError()
      }

      if (type === 'swarm_launch_progress') {
        const request_id = typeof payload?.request_id === 'string' ? payload.request_id : ''
        if (request_id) {
          get().updatePendingLaunch(request_id, {
            stage: typeof payload?.stage === 'string' ? payload.stage : undefined,
            message: typeof payload?.message === 'string' ? payload.message : undefined,
            provider: typeof payload?.provider === 'string' ? payload.provider : undefined,
            provider_id: typeof payload?.provider_id === 'string' ? payload.provider_id : undefined
          })
        }
      }

      if (type === 'command_rejected') {
        const { request_id, reason } = payload || {}
        if (request_id && get().pendingLaunches[request_id]) {
          get().removePendingLaunch(request_id)
          get().setLaunchError(reason || 'Launch failed.')
        }
      }

      if (type === 'reconcile') {
        get().setSwarms(payload)
      }

      if (type === 'status') {
        const swarm = get().swarms[payload.swarm_id]
        if (!swarm) return

        // Guard against malformed status payloads (e.g. error-only responses)
        if (!payload.status) return

        get().addOrUpdateSwarm({
          ...swarm,
          status: payload.status,
          slurm_state: payload.slurm_state
        })
      }

      if (type === 'queue_updated') {
        get().setInterSwarmQueue(payload?.items ?? [])
      }

      if (type === 'approvals_snapshot') {
        const snapshot = payload && typeof payload === 'object' ? payload : {}
        latestApprovalsSnapshot = snapshot as Record<string, any>
        const state = get()
        const updatedSwarms: Record<string, SwarmRecord> = { ...state.swarms }

        for (const [swarmId, swarm] of Object.entries(state.swarms)) {
          const parsedPending = parsePendingSnapshotForSwarm(swarmId)
          const nextPending = mergeSnapshotWithStickyPending(
            swarmId,
            parsedPending,
            swarm.pending_approvals
          )

          // Pending approvals are rendered from pending_approvals only. Clear any
          // stale per-turn approval state to avoid dual truth sources.
          const nextNodes: Record<number, NodeState> = { ...swarm.nodes }
          for (const [nodeIdRaw, node] of Object.entries(nextNodes)) {
            const nodeId = Number(nodeIdRaw)
            if (!Number.isFinite(nodeId)) continue
            let changed = false
            const turns = node.turns.map((t) => {
              if (!t.approval) return t
              changed = true
              return ({
                ...t,
                phase: t.phase === 'awaiting_approval' ? 'streaming' : t.phase,
                approval: undefined
                } as NodeTurn)
            })
            if (changed) {
              nextNodes[nodeId] = {
                ...node,
                turns: capTurns(
                  turns.filter((t) => {
                    const isSynthetic = t.injection_id.startsWith('approval-')
                    const hasContent =
                      (t.prompt ?? '').trim().length > 0 ||
                      (t.reasoning ?? '').trim().length > 0 ||
                      (t.deltas ?? []).some((d) => String(d ?? '').trim().length > 0)
                    return !(isSynthetic && !hasContent)
                  })
                )
              }
            }
          }

          updatedSwarms[swarmId] = {
            ...swarm,
            pending_approvals: nextPending,
            nodes: nextNodes
          }
        }

        set({ swarms: updatedSwarms })
        return
      }

      if (type === 'thread_status') {
        const swarm = get().swarms[payload.swarm_id]
        if (!swarm) return
        const nodeId = Number(payload.node_id)
        if (!Number.isFinite(nodeId)) return
        const node = swarm.nodes[nodeId]
        if (!node) return

        const status = payload?.status as { type?: string; activeFlags?: string[] } | undefined
        const activeFlags = Array.isArray(status?.activeFlags) ? status!.activeFlags : []
        const waitingOnApproval = activeFlags.includes('waitingOnApproval')
        const statusType = String(status?.type ?? '').toLowerCase()
        const isIdleStatus =
          statusType === 'idle' || statusType === 'complete' || statusType === 'completed'
        const targetInjectionId =
          typeof payload?.injection_id === 'string' && payload.injection_id
            ? payload.injection_id
            : undefined

        let turnsChanged = false
        let updatedTurns: NodeTurn[] = node.turns

        // Some runtimes emit idle/complete without assistant/task_complete.
        // Finalize the in-flight turn so the UI does not remain stuck.
        if (isIdleStatus && !waitingOnApproval) {
          let matchedByInjection = false
          updatedTurns = updatedTurns.map((t) => {
            if (t.phase === 'completed' || t.phase === 'error') return t
            if (targetInjectionId && t.injection_id === targetInjectionId) {
              matchedByInjection = true
              turnsChanged = true
              clearIdleCompletionTimer(payload.swarm_id, nodeId, t.injection_id)
              if (t.phase === 'awaiting_approval' || t.approval) {
                return ({
                  ...t,
                  phase: 'error',
                  approval: undefined,
                  error: 'Agent exited before approval flow completed.'
                } as NodeTurn)
              }
              if (!t.has_final_answer) {
                return ({
                  ...t,
                  phase: 'error',
                  approval: undefined,
                  error: 'Agent exited before sending a final answer.'
                } as NodeTurn)
              }
              return ({ ...t, phase: 'completed', approval: undefined } as NodeTurn)
            }
            return t
          })

          if (!matchedByInjection) {
            for (let i = updatedTurns.length - 1; i >= 0; i -= 1) {
              const turn = updatedTurns[i]
              if (turn.phase === 'completed' || turn.phase === 'error') continue
              const nextTurn =
                turn.phase === 'awaiting_approval' || turn.approval
                  ? ({
                      ...turn,
                      phase: 'error',
                      approval: undefined,
                      error: 'Agent exited before approval flow completed.'
                    } as NodeTurn)
                  : !turn.has_final_answer
                  ? ({
                      ...turn,
                      phase: 'error',
                      approval: undefined,
                      error: 'Agent exited before sending a final answer.'
                    } as NodeTurn)
                  : ({
                      ...turn,
                      phase: 'completed',
                      approval: undefined
                    } as NodeTurn)
              const turnsCopy = [...updatedTurns]
              turnsCopy[i] = nextTurn
              updatedTurns = turnsCopy
              turnsChanged = true
              clearIdleCompletionTimer(payload.swarm_id, nodeId, turn.injection_id)
              break
            }
          }
        }

        const pending = { ...(swarm.pending_approvals ?? {}) }
        let pendingChanged = false
        if (isIdleStatus && !waitingOnApproval) {
          if (Object.prototype.hasOwnProperty.call(pending, nodeId)) {
            delete pending[nodeId]
            pendingChanged = true
          }
        }

        if (turnsChanged || pendingChanged) {
          get().addOrUpdateSwarm({
            ...swarm,
            pending_approvals: pendingChanged ? pending : swarm.pending_approvals,
            nodes: {
              ...swarm.nodes,
              [nodeId]: { ...node, turns: capTurns(updatedTurns) }
            }
          })
        }

        return
      }

      if (type === 'turn_started') {
        const swarm = get().swarms[payload.swarm_id]
        if (!swarm) return

        const nodeId = Number(payload.node_id)
        const node = swarm.nodes[nodeId]
        if (!node) return
        if (node.turns.some((t) => t.injection_id === payload.injection_id)) {
          return
        }

        // Force-close any non-terminal turns to avoid multiple active bubbles
        const cleanedTurns: NodeTurn[] = node.turns.map((t) =>
          t.phase !== 'completed' && t.phase !== 'error'
            ? ({ ...t, phase: 'completed' } as NodeTurn)
            : t
        )

        const turns = [...cleanedTurns]

        // Replace only the last provisional turn if present
        const newTurn: NodeTurn = {
          injection_id: payload.injection_id,
          prompt: typeof payload.prompt === 'string' ? payload.prompt : '',
          deltas: [],
          reasoning: '',
          phase: 'streaming',
          has_final_answer: false
        }

        if (pendingComplete[payload.injection_id]) {
          delete pendingComplete[payload.injection_id]
        }

        if (pendingReasoning[payload.injection_id]) {
          newTurn.reasoning = pendingReasoning[payload.injection_id]
          delete pendingReasoning[payload.injection_id]
        } else if (pendingReasoningDeltas[payload.injection_id]) {
          newTurn.reasoning = pendingReasoningDeltas[payload.injection_id].join('')
          delete pendingReasoningDeltas[payload.injection_id]
        }

        if (Object.prototype.hasOwnProperty.call(pendingTaskComplete, payload.injection_id)) {
          const completed = pendingTaskComplete[payload.injection_id]
          const completedContent = completed?.content
          newTurn.phase = 'completed'
          newTurn.has_final_answer = true
          if (completedContent && newTurn.deltas.length === 0) {
            newTurn.deltas = [completedContent]
          }
          delete pendingTaskComplete[payload.injection_id]
        }

        if (
          turns.length > 0 &&
          turns[turns.length - 1].injection_id.startsWith('temp-')
        ) {
          const provisional = turns[turns.length - 1]
          turns[turns.length - 1] = {
            ...newTurn,
            prompt: provisional.prompt || newTurn.prompt,
            reasoning: provisional.reasoning
          } as NodeTurn
        } else {
          turns.push(newTurn)
        }

        get().addOrUpdateSwarm({
          ...swarm,
          nodes: {
            ...swarm.nodes,
            [nodeId]: {
              ...node,
              turns: capTurns(turns)
            }
          }
        })

        set({ pendingPrompt: undefined })
      }

      if (type === 'delta') {
        const swarm = get().swarms[payload.swarm_id]
        if (!swarm) return

        const nodeId = Number(payload.node_id)
        const node = swarm.nodes[nodeId]
        if (!node) return

        const turns = [...node.turns]
        const turn = turns.find(
          (t) => t.injection_id === payload.injection_id
        )

        if (!turn) {
          // Ignore assistant deltas until turn_started establishes the
          // authoritative injection_id mapping for this bubble.
          return
        }

        turn.deltas = [...turn.deltas, payload.content]
        // If stream resumes after an idle auto-complete, reopen as streaming.
        if (turn.phase === 'completed') {
          turn.phase = 'streaming'
        }
        scheduleIdleCompletion(payload.swarm_id, nodeId, payload.injection_id)

        get().addOrUpdateSwarm({
          ...swarm,
          nodes: {
            ...swarm.nodes,
            [nodeId]: {
              ...node,
              turns: capTurns(turns)
            }
          }
        })
      }

      if (type === 'assistant') {
        const swarm = get().swarms[payload.swarm_id]
        if (!swarm) return

        const nodeId = Number(payload.node_id)
        const node = swarm.nodes[nodeId]
        if (!node) return

        const turns = [...node.turns]
        const turn = turns.find(
          (t) => t.injection_id === payload.injection_id
        )

        if (!turn) {
          // Ignore assistant snapshots until turn_started establishes the
          // authoritative injection_id mapping for this bubble.
          return
        }

        const snapshot = typeof payload.content === 'string' ? payload.content : ''
        const streamed = turn.deltas.join('')
        // Prefer the terminal assistant snapshot when it is more complete than
        // accumulated deltas (which can occasionally miss chunks under load).
        if (!streamed) {
          turn.deltas = snapshot ? [snapshot] : turn.deltas
        } else if (snapshot && snapshot.length > streamed.length) {
          turn.deltas = [snapshot]
        }
        const isFinalAnswer = payload?.final_answer === true
        if (isFinalAnswer) {
          turn.phase = 'completed'
          turn.approval = undefined
          turn.has_final_answer = true
          clearIdleCompletionTimer(payload.swarm_id, nodeId, payload.injection_id)
        } else if (turn.phase === 'completed') {
          turn.phase = 'streaming'
        }

        get().addOrUpdateSwarm({
          ...swarm,
          // Do not clear pending approvals by injection_id here. A single turn
          // can emit multiple approval requests, and clearing by injection_id
          // causes still-pending sibling approvals to disappear from the panel.
          pending_approvals: swarm.pending_approvals,
          nodes: {
            ...swarm.nodes,
            [nodeId]: {
              ...node,
              turns: capTurns(turns)
            }
          }
        })
      }

      if (type === 'turn_complete') {
        const swarm = get().swarms[payload.swarm_id]
        if (!swarm) return

        const nodeId = Number(payload.node_id)
        const node = swarm.nodes[nodeId]
        if (!node) return

        const turns = [...node.turns]
        const turn = turns.find(
          (t) => t.injection_id === payload.injection_id
        )

        if (!turn) {
          return
        }

        turn.phase = 'completed'
        turn.approval = undefined
        turn.has_final_answer = true
        clearIdleCompletionTimer(payload.swarm_id, nodeId, payload.injection_id)

        get().addOrUpdateSwarm({
          ...swarm,
          // Keep approval state authoritative via explicit approval events and
          // /approvals snapshots. turn_complete should not drop other pending
          // approvals from the same injection.
          pending_approvals: swarm.pending_approvals,
          nodes: {
            ...swarm.nodes,
            [nodeId]: {
              ...node,
              turns
            }
          }
        })
      }

      if (type === 'task_complete') {
        const swarm = get().swarms[payload.swarm_id]
        if (!swarm) return

        const nodeId = Number(payload.node_id)
        const node = swarm.nodes[nodeId]
        if (!node) return

        const turns = [...node.turns]
        const turnIndex = turns.findIndex(
          (t) => t.injection_id === payload.injection_id
        )
        const rawContent =
          typeof payload.last_agent_message === 'string'
            ? payload.last_agent_message
            : typeof payload.content === 'string'
            ? payload.content
            : undefined

        const previousTurn = [...turns]
          .reverse()
          .find((t) => t.injection_id !== payload.injection_id && t.phase === 'completed')

        const previousRaw = previousTurn?.deltas?.join('').trim() ?? ''

        // Some runtimes emit cumulative final text that includes the prior turn.
        // If the final content starts with previous completed output, strip it.
        const content =
          rawContent &&
          previousRaw &&
          rawContent.trim().startsWith(previousRaw)
            ? rawContent.trim().slice(previousRaw.length).trimStart()
            : rawContent

        if (turnIndex >= 0) {
          const existing = turns[turnIndex]
          turns[turnIndex] = ({
            ...existing,
            phase: 'completed',
            approval: undefined,
            has_final_answer: true,
            // Keep streamed content as source of truth. task_complete payload can
            // be cumulative and may include prior turn text.
            deltas:
              existing.deltas.length > 0
                ? existing.deltas
                : content
                ? [content]
                : existing.deltas
          } as NodeTurn)
          clearIdleCompletionTimer(payload.swarm_id, nodeId, payload.injection_id)
        } else {
          // Do not overwrite provisional temp turns with mismatched injection IDs.
          // Late task_complete from a prior turn can otherwise pollute the current
          // working bubble. Cache until the matching turn_started arrives.
          pendingTaskComplete[payload.injection_id] = { content }
          return
        }

        get().addOrUpdateSwarm({
          ...swarm,
          // Keep approval state authoritative via explicit approval events and
          // /approvals snapshots. task_complete should not drop other pending
          // approvals from the same injection.
          pending_approvals: swarm.pending_approvals,
          nodes: {
            ...swarm.nodes,
            [nodeId]: {
              ...node,
              turns
            }
          }
        })
      }

      if (
        type === 'reasoning_delta' ||
        type === 'agent_reasoning_delta' ||
        type === 'reasoning_content_delta'
      ) {
        const swarm = get().swarms[payload.swarm_id]
        if (!swarm) return
        const nodeId = Number(payload.node_id)
        const node = swarm.nodes[nodeId]
        if (!node) return

        const delta = payload.content ?? payload.msg?.delta
        const turnExists = node.turns.some((t) => t.injection_id === payload.injection_id)

        if (!turnExists) {
          if (!pendingReasoningDeltas[payload.injection_id]) {
            pendingReasoningDeltas[payload.injection_id] = []
          }
          pendingReasoningDeltas[payload.injection_id].push(delta ?? '')
          return
        }

        const updatedTurns: NodeTurn[] = node.turns.map((t) =>
          t.injection_id === payload.injection_id
            ? { ...t, reasoning: (t.reasoning ?? '') + (delta ?? '') }
            : t
        )

        get().addOrUpdateSwarm({
          ...swarm,
          pending_approvals: removePendingApprovalByCallId(
            swarm.pending_approvals,
            payload.call_id,
            nodeId
          ),
          nodes: {
            ...swarm.nodes,
            [nodeId]: { ...node, turns: updatedTurns }
          }
        })
      }

      if (type === 'reasoning' || type === 'agent_reasoning') {
        const swarm = get().swarms[payload.swarm_id]
        if (!swarm) return
        const nodeId = Number(payload.node_id)
        const node = swarm.nodes[nodeId]
        if (!node) return

        const text = payload.content ?? payload.msg?.text
        const turnExists = node.turns.some((t) => t.injection_id === payload.injection_id)

        if (!turnExists) {
          pendingReasoning[payload.injection_id] = text ?? ''
          return
        }

        const updatedTurns = node.turns.map((t) =>
          t.injection_id === payload.injection_id
            ? { ...t, reasoning: text ?? '' }
            : t
        )

        get().addOrUpdateSwarm({
          ...swarm,
          nodes: {
            ...swarm.nodes,
            [nodeId]: { ...node, turns: capTurns(updatedTurns) }
          }
        })
      }

      if (type === 'exec_approval_required') {
        const state = get()
        let swarm: SwarmRecord | undefined = state.swarms[payload.swarm_id]
        if (!swarm && typeof payload?.job_id === 'string') {
          swarm = Object.values(state.swarms).find((s) => s.job_id === payload.job_id)
        }
        if (!swarm) return

        const nodeId = Number(payload.node_id)
        if (!Number.isFinite(nodeId)) return
        const callId = typeof payload.call_id === 'string' ? payload.call_id : ''
        if (!callId) return
        if (isApprovalRecentlyResolved(swarm.swarm_id, nodeId, callId)) {
          return
        }

        const existingPendingForNode = (swarm.pending_approvals ?? {})[nodeId] ?? []
        const incomingPending: PendingApproval = {
          call_id: callId,
          injection_id: payload.injection_id,
          command: payload.command,
          reason: payload.reason,
          cwd: payload.cwd,
          proposed_execpolicy_amendment: payload.proposed_execpolicy_amendment,
          available_decisions: payload.available_decisions,
          created_at_ms: Date.now(),
          updated_at_ms: Date.now(),
          status: 'pending'
        }
        const existing = existingPendingForNode.find((a) => a.call_id === payload.call_id)
        const nextPendingForNode = isIncomingApprovalNewer(existing, incomingPending)
          ? [
              ...existingPendingForNode.filter((a) => a.call_id !== payload.call_id),
              incomingPending
            ]
          : existingPendingForNode

        get().addOrUpdateSwarm({
          ...swarm,
          pending_approvals: {
            ...(swarm.pending_approvals ?? {}),
            [nodeId]: nextPendingForNode
          }
        })
        clearIdleCompletionTimer(swarm.swarm_id, nodeId, payload.injection_id)
      }

      if (type === 'exec_approval_resolved') {
        const swarm = Object.values(get().swarms).find(
          (s) => s.job_id === payload.job_id
        )
        if (!swarm) return

        const updatedNodes = { ...swarm.nodes }
        const resolvedNodeId = Number(payload.node_id)
        const hasResolvedNode = Number.isFinite(resolvedNodeId)
        if (typeof payload.call_id === 'string' && payload.call_id) {
          if (hasResolvedNode) {
            markApprovalResolved(swarm.swarm_id, resolvedNodeId, payload.call_id)
          } else {
            for (const nodeKey of Object.keys(updatedNodes)) {
              const nodeId = Number(nodeKey)
              if (!Number.isFinite(nodeId)) continue
              markApprovalResolved(swarm.swarm_id, nodeId, payload.call_id)
            }
          }
        }
        const currentPolicies = swarm.known_exec_policies ?? []
        const newAmendment = payload.approved
          ? extractExecPolicyAmendment(payload.decision)
          : undefined
        const hasPolicy = Array.isArray(newAmendment) && newAmendment.length > 0
        const keyOf = (rule: string[]) => JSON.stringify(rule)
        const policySeen = new Set(currentPolicies.map((p) => keyOf(p)))
        const nextPolicies = hasPolicy && !policySeen.has(keyOf(newAmendment as string[]))
          ? [...currentPolicies, newAmendment as string[]]
          : currentPolicies

        const updatedPendingApprovals = markPendingApprovalStatus(
          swarm.pending_approvals,
          payload.call_id,
          'resolved',
          hasResolvedNode ? resolvedNodeId : undefined
        )

        get().addOrUpdateSwarm({
          ...swarm,
          known_exec_policies: nextPolicies,
          pending_approvals: updatedPendingApprovals,
          nodes: updatedNodes
        })
      }

      if (type === 'command_started') {
        const swarm = get().swarms[payload.swarm_id]
        if (!swarm) return
        const nodeId = Number(payload.node_id)
        const node = swarm.nodes[nodeId]
        if (!node) return
        if (typeof payload.call_id === 'string' && payload.call_id) {
          markApprovalResolved(swarm.swarm_id, nodeId, payload.call_id)
        }

        const updatedTurns = node.turns.map((t) =>
          t.injection_id === payload.injection_id
            ? ({
                ...t,
                // Command start is definitive that approval has been granted.
                phase: 'executing',
                approval:
                  t.approval?.call_id === payload.call_id ? undefined : t.approval,
                execution: {
                  call_id: payload.call_id,
                  command: payload.command,
                  cwd: payload.cwd,
                  started_at: Date.now(),
                  status: 'running'
                }
              } as NodeTurn)
            : t
        )

        get().addOrUpdateSwarm({
          ...swarm,
          pending_approvals: markPendingApprovalStatus(
            swarm.pending_approvals,
            payload.call_id,
            'started',
            nodeId
          ),
          nodes: {
            ...swarm.nodes,
            [nodeId]: { ...node, turns: updatedTurns }
          }
        })
        clearIdleCompletionTimer(payload.swarm_id, nodeId, payload.injection_id)
      }

      if (type === 'command_completed') {
        const swarm = get().swarms[payload.swarm_id]
        if (!swarm) return
        const nodeId = Number(payload.node_id)
        const node = swarm.nodes[nodeId]
        if (!node) return

        const updatedTurns: NodeTurn[] = node.turns.map((t) => {
          if (t.injection_id !== payload.injection_id) return t
          if (!t.execution) return t

          return ({
            ...t,
            // Do not hide pending approval on command completion unless we
            // received explicit exec_approval_resolved.
            phase: t.approval?.call_id ? 'awaiting_approval' : 'streaming',
            execution: {
              ...t.execution,
              status: 'completed',
              completed_at: Date.now(),
              stdout: payload.stdout,
              stderr: payload.stderr,
              exit_code: payload.exit_code
            }
          } as NodeTurn)
        })

        get().addOrUpdateSwarm({
          ...swarm,
          nodes: {
            ...swarm.nodes,
            [nodeId]: { ...node, turns: updatedTurns }
          }
        })
      }

      if (type === 'agent_error') {
        const swarm = get().swarms[payload.swarm_id]
        if (!swarm) return
        const nodeId = Number(payload.node_id)
        const node = swarm.nodes[nodeId]
        if (!node) return

        const updatedTurns: NodeTurn[] = node.turns.map((t) =>
          t.injection_id === payload.injection_id
            ? ({ ...t, phase: 'error', error: payload.message } as NodeTurn)
            : t
        )

        get().addOrUpdateSwarm({
          ...swarm,
          nodes: {
            ...swarm.nodes,
            [nodeId]: { ...node, turns: updatedTurns }
          }
        })
        clearIdleCompletionTimer(payload.swarm_id, nodeId, payload.injection_id)
      }

      if (type === 'usage') {
        const swarm = get().swarms[payload.swarm_id]
        if (!swarm) return
        const nodeId = Number(payload.node_id)
        const node = swarm.nodes[nodeId]
        if (!node) return

        const usage: TokenUsage = {
          total_tokens: Number(payload.total_tokens ?? 0),
          input_tokens: typeof payload.input_tokens === 'number' ? payload.input_tokens : undefined,
          cached_input_tokens: typeof payload.cached_input_tokens === 'number' ? payload.cached_input_tokens : undefined,
          output_tokens: typeof payload.output_tokens === 'number' ? payload.output_tokens : undefined,
          reasoning_output_tokens:
            typeof payload.reasoning_output_tokens === 'number' ? payload.reasoning_output_tokens : undefined,
          last_total_tokens: typeof payload.last_total_tokens === 'number' ? payload.last_total_tokens : undefined,
          last_input_tokens: typeof payload.last_input_tokens === 'number' ? payload.last_input_tokens : undefined,
          last_cached_input_tokens:
            typeof payload.last_cached_input_tokens === 'number' ? payload.last_cached_input_tokens : undefined,
          last_output_tokens: typeof payload.last_output_tokens === 'number' ? payload.last_output_tokens : undefined,
          last_reasoning_output_tokens:
            typeof payload.last_reasoning_output_tokens === 'number'
              ? payload.last_reasoning_output_tokens
              : undefined,
          model_context_window:
            typeof payload.model_context_window === 'number' ? payload.model_context_window : undefined,
          usage_source: typeof payload.usage_source === 'string' ? payload.usage_source : undefined
        }

        const updatedTurns: NodeTurn[] = node.turns.map((t) =>
          t.injection_id === payload.injection_id
            ? ({ ...t, usage } as NodeTurn)
            : t
        )

        get().addOrUpdateSwarm({
          ...swarm,
          nodes: {
            ...swarm.nodes,
            [nodeId]: { ...node, turns: updatedTurns }
          }
        })
      }

      if (type === 'swarm_removed') {
        get().removeSwarm(payload.swarm_id)
      }
    }
  }
}, {
  name: 'codeswarm-ui-store-v1',
  partialize: (state) => ({
    swarms: state.swarms,
    selectedSwarm: state.selectedSwarm,
    activeNodeBySwarm: state.activeNodeBySwarm
  })
}))
