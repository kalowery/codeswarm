import { create } from 'zustand'
import { persist } from 'zustand/middleware'

const MAX_TURNS_PER_NODE = 300
const RESOLVED_APPROVAL_TTL_MS = 120000
const PENDING_APPROVAL_STICKY_MS = 10000
const RESOLVED_APPROVAL_STICKY_MS = 5000
const MAX_EXEC_OUTPUT_CHARS = 16000
const EXEC_OUTPUT_HEAD_CHARS = 10000
const EXEC_OUTPUT_TAIL_CHARS = 5000

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
  stdout_truncated?: boolean
  stderr_truncated?: boolean
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
  approval_id?: string
  approval_status?: string
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

export interface NodeSystemEvent {
  id: string
  ts: number
  level: 'info' | 'warn' | 'error'
  message: string
}

export interface NodeState {
  node_id: number
  turns: NodeTurn[]
  system_events?: NodeSystemEvent[]
}

const capTurns = (turns: NodeTurn[]) =>
  turns.length > MAX_TURNS_PER_NODE
    ? turns.slice(-MAX_TURNS_PER_NODE)
    : turns

const MAX_SYSTEM_EVENTS_PER_NODE = 20

const capSystemEvents = (events: NodeSystemEvent[] | undefined) =>
  (events ?? []).length > MAX_SYSTEM_EVENTS_PER_NODE
    ? (events ?? []).slice(-MAX_SYSTEM_EVENTS_PER_NODE)
    : (events ?? [])

const truncateExecutionOutput = (value: unknown): { text?: string; truncated?: boolean } => {
  if (typeof value !== 'string') return {}
  if (value.length <= MAX_EXEC_OUTPUT_CHARS) {
    return { text: value, truncated: false }
  }
  const head = value.slice(0, EXEC_OUTPUT_HEAD_CHARS)
  const tail = value.slice(-EXEC_OUTPUT_TAIL_CHARS)
  const omitted = value.length - head.length - tail.length
  return {
    text:
      `${head}\n\n[... truncated ${omitted.toLocaleString()} characters ...]\n\n${tail}`,
    truncated: true
  }
}

export interface SwarmRecord {
  swarm_id: string
  alias: string
  job_id: string
  node_count: number
  status: string
  slurm_state?: string
  termination_message?: string
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

export interface ProjectTaskRecord {
  task_id: string
  title: string
  prompt: string
  acceptance_criteria?: string[]
  depends_on?: string[]
  owned_paths?: string[]
  expected_touch_paths?: string[]
  status: string
  attempts?: number
  assigned_swarm_id?: string
  assigned_node_id?: number
  assignment_injection_id?: string
  branch?: string
  result_status?: string
  result_raw?: string
  last_error?: string
  beads_id?: string
  beads_sync_status?: string
  beads_last_error?: string
  created_at?: number
  updated_at?: number
}

export interface ProjectRecord {
  project_id: string
  title: string
  repo_path: string
  base_branch?: string
  worker_swarm_ids?: string[]
  status: string
  workspace_subdir?: string
  task_order?: string[]
  task_counts?: Record<string, number>
  tasks: Record<string, ProjectTaskRecord>
  created_at?: number
  updated_at?: number
  last_error?: string
  beads_sync_status?: string
  beads_last_error?: string
  beads_repo_path?: string
  beads_prefix?: string
  beads_db_path?: string
  beads_root_id?: string
}

interface SwarmStore {
  swarms: Record<string, SwarmRecord>
  projects: Record<string, ProjectRecord>
  interSwarmQueue: InterSwarmQueueItem[]
  pendingLaunches: Record<string, PendingLaunchRecord>
  selectedSwarm?: string
  selectedProject?: string
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
  setProjects: (projects: Record<string, ProjectRecord> | ProjectRecord[] | any) => void
  setInterSwarmQueue: (items: InterSwarmQueueItem[]) => void
  addOrUpdateSwarm: (swarm: SwarmRecord) => void
  removeSwarm: (swarm_id: string) => void
  selectSwarm: (swarm_id: string) => void
  selectProject: (project_id?: string) => void
  handleMessage: (msg: any) => void
}

export const useSwarmStore = create<SwarmStore>()(persist((set, get) => {
  // Track completions that arrive before turn_started
  const pendingComplete: Record<string, boolean> = {} // deprecated
  const pendingReasoningDeltas: Record<string, string[]> = {}
  const pendingReasoning: Record<string, string> = {}
  const pendingTaskComplete: Record<string, { content?: string }> = {}
  const recentEventFingerprints = new Map<string, number>()
  let latestApprovalsSnapshot: Record<string, any> = {}
  let latestApprovalsVersion = 0
  const resolvedApprovalsUntil: Record<string, number> = {}
  const RECENT_EVENT_TTL_MS = 60_000

  const pruneRecentEventFingerprints = () => {
    const cutoff = Date.now() - RECENT_EVENT_TTL_MS
    for (const [key, ts] of recentEventFingerprints.entries()) {
      if (ts < cutoff) {
        recentEventFingerprints.delete(key)
      }
    }
  }

  const stableSerialize = (value: unknown): string => {
    if (value === null || value === undefined) return String(value)
    if (typeof value !== 'object') return JSON.stringify(value)
    if (Array.isArray(value)) return `[${value.map((v) => stableSerialize(v)).join(',')}]`
    const entries = Object.entries(value as Record<string, unknown>).sort(([a], [b]) =>
      a.localeCompare(b)
    )
    return `{${entries
      .map(([k, v]) => `${JSON.stringify(k)}:${stableSerialize(v)}`)
      .join(',')}}`
  }

  const eventFingerprint = (type: string, payload: any): string | null => {
    if (!payload || typeof payload !== 'object') return null
    const swarmId = String(payload.swarm_id ?? payload.job_id ?? '')
    const nodeId = Number.isFinite(Number(payload.node_id)) ? Number(payload.node_id) : ''
    const injectionId = typeof payload.injection_id === 'string' ? payload.injection_id : ''
    const callId = typeof payload.call_id === 'string' ? payload.call_id : ''

    if (type === 'turn_started' || type === 'turn_complete' || type === 'task_complete') {
      return `${type}:${swarmId}:${nodeId}:${injectionId}`
    }
    if (type === 'command_started' || type === 'command_completed') {
      return `${type}:${swarmId}:${nodeId}:${injectionId}:${callId}:${stableSerialize(payload.command)}:${stableSerialize(payload.exit_code)}:${stableSerialize(payload.status)}`
    }
    if (type === 'exec_approval_required') {
      return `${type}:${String(payload.job_id ?? '')}:${nodeId}:${callId}:${String(payload.approval_id ?? '')}:${stableSerialize(payload.command)}:${stableSerialize(payload.available_decisions)}`
    }
    if (type === 'exec_approval_resolved') {
      return `${type}:${String(payload.job_id ?? '')}:${nodeId}:${callId}:${stableSerialize(payload.decision)}:${stableSerialize(payload.approved)}`
    }
    return null
  }

  const shouldSuppressDuplicateEvent = (type: string, payload: any) => {
    const fingerprint = eventFingerprint(type, payload)
    if (!fingerprint) return false
    pruneRecentEventFingerprints()
    if (recentEventFingerprints.has(fingerprint)) {
      return true
    }
    recentEventFingerprints.set(fingerprint, Date.now())
    return false
  }

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
          approval_id: typeof a.approval_id === 'string' ? a.approval_id : undefined,
          approval_status: typeof a.approval_status === 'string' ? a.approval_status : undefined,
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

  const clearIdleCompletionTimer = (_swarmId: string, _nodeId: number, _injectionId: string) => {}
  const scheduleIdleCompletion = (_swarmId: string, _nodeId: number, _injectionId: string) => {}
  const findSwarmForPayload = (payload: any): SwarmRecord | undefined => {
    if (typeof payload?.swarm_id === 'string' && get().swarms[payload.swarm_id]) {
      return get().swarms[payload.swarm_id]
    }
    if (typeof payload?.job_id === 'string') {
      return Object.values(get().swarms).find((s) => s.job_id === payload.job_id)
    }
    return undefined
  }
  const appendNodeSystemEvent = (
    swarm: SwarmRecord,
    nodeId: number,
    event: NodeSystemEvent
  ) => {
    const node = swarm.nodes[nodeId]
    if (!node) return
    const existing = node.system_events ?? []
    const last = existing[existing.length - 1]
    if (last && last.message === event.message && event.ts - last.ts < 5000) {
      return
    }
    get().addOrUpdateSwarm({
      ...swarm,
      nodes: {
        ...swarm.nodes,
        [nodeId]: {
          ...node,
          system_events: capSystemEvents([...existing, event])
        }
      }
    })
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

  const attachApprovalToNodeTurns = (
    node: NodeState,
    approval: PendingApproval
  ): NodeState => {
    let matched = false
    const turns = node.turns.map((t) => {
      const injectionMatches =
        approval.injection_id &&
        typeof t.injection_id === 'string' &&
        t.injection_id === approval.injection_id
      const alreadyMatches = t.approval?.call_id === approval.call_id
      if (!injectionMatches && !alreadyMatches) return t
      matched = true
      return {
        ...t,
        phase: t.phase === 'completed' ? t.phase : 'awaiting_approval',
        approval: {
          call_id: approval.call_id,
          command: approval.command,
          reason: approval.reason,
          cwd: approval.cwd,
          proposed_execpolicy_amendment: approval.proposed_execpolicy_amendment,
          available_decisions: approval.available_decisions
        }
      } as NodeTurn
    })

    if (matched) {
      return { ...node, turns: capTurns(turns) }
    }

    const syntheticTurn: NodeTurn = {
      injection_id: `approval-${approval.call_id}`,
      prompt: '',
      deltas: [],
      reasoning: '',
      phase: 'awaiting_approval',
      has_final_answer: false,
      approval: {
        call_id: approval.call_id,
        command: approval.command,
        reason: approval.reason,
        cwd: approval.cwd,
        proposed_execpolicy_amendment: approval.proposed_execpolicy_amendment,
        available_decisions: approval.available_decisions
      }
    }
    return { ...node, turns: capTurns([...node.turns, syntheticTurn]) }
  }

  const clearApprovalFromNodeTurns = (
    node: NodeState,
    callId: string | undefined
  ): NodeState => {
    if (!callId) return node
    const turns = node.turns
      .map((t) => {
        if (t.approval?.call_id !== callId) return t
        return {
          ...t,
          phase: t.phase === 'awaiting_approval' ? 'streaming' : t.phase,
          approval: undefined
        } as NodeTurn
      })
      .filter((t) => {
        const isSynthetic = t.injection_id.startsWith('approval-')
        const hasContent =
          (t.prompt ?? '').trim().length > 0 ||
          (t.reasoning ?? '').trim().length > 0 ||
          (t.deltas ?? []).some((d) => String(d ?? '').trim().length > 0)
        return !(isSynthetic && !t.approval && !hasContent)
      })
    return { ...node, turns: capTurns(turns) }
  }

  const syncNodeTurnApprovals = (
    node: NodeState,
    approvals: PendingApproval[] | undefined
  ): NodeState => {
    const activeCallIds = new Set((approvals ?? []).map((a) => a.call_id).filter(Boolean))
    const turns = node.turns
      .map((t) => {
        if (!t.approval?.call_id) return t
        if (activeCallIds.has(t.approval.call_id)) return t
        return {
          ...t,
          phase: t.phase === 'awaiting_approval' ? 'streaming' : t.phase,
          approval: undefined
        } as NodeTurn
      })
      .filter((t) => {
        const isSynthetic = t.injection_id.startsWith('approval-')
        const hasContent =
          (t.prompt ?? '').trim().length > 0 ||
          (t.reasoning ?? '').trim().length > 0 ||
          (t.deltas ?? []).some((d) => String(d ?? '').trim().length > 0)
        return !(isSynthetic && !t.approval && !hasContent)
      })
    return { ...node, turns: capTurns(turns) }
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
    projects: {},
    interSwarmQueue: [],
    pendingLaunches: {},
    selectedSwarm: undefined,
    selectedProject: undefined,
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

    setProjects: (projectsInput) => {
      set((state) => {
        const updated: Record<string, ProjectRecord> = {}
        if (Array.isArray(projectsInput)) {
          projectsInput.forEach((project) => {
            if (project?.project_id) {
              updated[project.project_id] = project
            }
          })
        } else if (projectsInput && typeof projectsInput === 'object') {
          Object.entries(projectsInput).forEach(([projectId, project]) => {
            if (project && typeof project === 'object') {
              updated[projectId] = project as ProjectRecord
            }
          })
        }
        const selectedProject =
          state.selectedProject && updated[state.selectedProject]
            ? state.selectedProject
            : Object.keys(updated)[0]
        return {
          projects: updated,
          selectedProject
        }
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

    selectProject: (project_id) => set({ selectedProject: project_id }),

    handleMessage: (msg) => {
      const { type, payload } = msg

      if (shouldSuppressDuplicateEvent(type, payload)) {
        return
      }

      if (type === 'project_list' || type === 'projects_updated') {
        get().setProjects(payload?.projects ?? {})
        return
      }

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
          slurm_state: payload.slurm_state,
          termination_message:
            String(payload.status || '').toLowerCase() === 'terminating'
              ? swarm.termination_message
              : undefined
        })
      }

      if (type === 'swarm_terminate_progress') {
        const swarmId = typeof payload?.swarm_id === 'string' ? payload.swarm_id : ''
        const message = typeof payload?.message === 'string' ? payload.message : ''
        if (!swarmId) return
        const swarm = get().swarms[swarmId]
        if (!swarm) return
        get().addOrUpdateSwarm({
          ...swarm,
          termination_message: message || swarm.termination_message
        })
      }

      if (type === 'queue_updated') {
        get().setInterSwarmQueue(payload?.items ?? [])
      }

      if (type === 'approvals_snapshot') {
        const snapshotRoot = payload && typeof payload === 'object' ? payload : {}
        const incomingVersionRaw = (snapshotRoot as any).approvals_version
        const incomingVersion = Number(incomingVersionRaw)
        if (Number.isFinite(incomingVersion) && incomingVersion < latestApprovalsVersion) {
          return
        }
        if (Number.isFinite(incomingVersion)) {
          latestApprovalsVersion = incomingVersion
        } else {
          latestApprovalsVersion += 1
        }
        const snapshot =
          (snapshotRoot as any).approvals && typeof (snapshotRoot as any).approvals === 'object'
            ? ((snapshotRoot as any).approvals as Record<string, any>)
            : (snapshotRoot as Record<string, any>)
        latestApprovalsSnapshot = snapshot
        const state = get()
        const updatedSwarms: Record<string, SwarmRecord> = { ...state.swarms }

        for (const [swarmId, swarm] of Object.entries(state.swarms)) {
          const parsedPending = parsePendingSnapshotForSwarm(swarmId)
          const nextPending = parsedPending
          const nextNodes: Record<number, NodeState> = {}
          for (const [nodeKey, node] of Object.entries(swarm.nodes)) {
            const nodeId = Number(nodeKey)
            nextNodes[nodeId] = syncNodeTurnApprovals(node, nextPending[nodeId] ?? [])
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
        // A late delta should restore streaming if earlier terminal markers
        // arrived out of order.
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
        const isFinalAnswer = payload?.final_answer === true

        if (
          snapshot &&
          streamed === snapshot &&
          ((isFinalAnswer && turn.phase === 'completed') || (!isFinalAnswer && turn.phase !== 'completed'))
        ) {
          return
        }

        // Prefer the terminal assistant snapshot when it is more complete than
        // accumulated deltas (which can occasionally miss chunks under load).
        if (!streamed) {
          turn.deltas = snapshot ? [snapshot] : turn.deltas
        } else if (snapshot && snapshot.length > streamed.length) {
          turn.deltas = [snapshot]
        }
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

        if (turn.phase === 'completed' && turn.has_final_answer === true && !turn.approval) {
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
          const existingRaw = existing.deltas.join('')
          if (
            existing.phase === 'completed' &&
            existing.has_final_answer === true &&
            (!content || !existingRaw || existingRaw === content)
          ) {
            clearIdleCompletionTimer(payload.swarm_id, nodeId, payload.injection_id)
            return
          }
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
        const node = swarm.nodes[nodeId]
        if (!node) return
        const callId = typeof payload.call_id === 'string' ? payload.call_id : ''
        if (!callId) return
        if (isApprovalRecentlyResolved(swarm.swarm_id, nodeId, callId)) {
          return
        }

        const existingPendingForNode = (swarm.pending_approvals ?? {})[nodeId] ?? []
        const incomingPending: PendingApproval = {
          approval_id: typeof payload.approval_id === 'string' ? payload.approval_id : undefined,
          approval_status: typeof payload.approval_status === 'string' ? payload.approval_status : undefined,
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
        if (
          existing &&
          !isIncomingApprovalNewer(existing, incomingPending) &&
          node.turns.some((t) => t.approval?.call_id === callId)
        ) {
          return
        }
        const nextPendingForNode = isIncomingApprovalNewer(existing, incomingPending)
          ? [
              ...existingPendingForNode.filter((a) => a.call_id !== payload.call_id),
              incomingPending
            ]
          : existingPendingForNode
        const updatedNode = attachApprovalToNodeTurns(node, incomingPending)

        get().addOrUpdateSwarm({
          ...swarm,
          pending_approvals: {
            ...(swarm.pending_approvals ?? {}),
            [nodeId]: nextPendingForNode
          },
          nodes: {
            ...swarm.nodes,
            [nodeId]: updatedNode
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
        if (typeof payload.call_id === 'string' && payload.call_id) {
          if (hasResolvedNode && updatedNodes[resolvedNodeId]) {
            updatedNodes[resolvedNodeId] = clearApprovalFromNodeTurns(
              updatedNodes[resolvedNodeId],
              payload.call_id
            )
          } else {
            for (const [nodeKey, node] of Object.entries(updatedNodes)) {
              const nodeId = Number(nodeKey)
              if (!Number.isFinite(nodeId)) continue
              updatedNodes[nodeId] = clearApprovalFromNodeTurns(node, payload.call_id)
            }
          }
        }

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

        let changed = false
        const updatedTurns = node.turns.map((t) => {
          if (t.injection_id !== payload.injection_id) return t
          const existingExecution = t.execution
          const sameExecution =
            existingExecution?.call_id === payload.call_id &&
            existingExecution?.status === 'running' &&
            JSON.stringify(existingExecution?.command) === JSON.stringify(payload.command) &&
            existingExecution?.cwd === payload.cwd
          const approvalAlreadyCleared = t.approval?.call_id !== payload.call_id
          if (sameExecution && approvalAlreadyCleared && t.phase === 'executing') {
            return t
          }
          changed = true
          return ({
            ...t,
            // Command start is definitive that approval has been granted.
            phase: 'executing',
            approval:
              t.approval?.call_id === payload.call_id ? undefined : t.approval,
            execution: {
              call_id: payload.call_id,
              command: payload.command,
              cwd: payload.cwd,
              started_at:
                existingExecution && existingExecution.call_id === payload.call_id
                  ? existingExecution.started_at ?? Date.now()
                  : Date.now(),
              status: 'running'
            }
          } as NodeTurn)
        })
        if (!changed) return
        const clearedNode = clearApprovalFromNodeTurns(
          { ...node, turns: updatedTurns },
          payload.call_id
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
            [nodeId]: clearedNode
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

        let changed = false
        const updatedTurns: NodeTurn[] = node.turns.map((t) => {
          if (t.injection_id !== payload.injection_id) return t
          if (!t.execution) return t

          const alreadyApplied =
            t.execution.call_id === payload.call_id &&
            t.execution.status === 'completed' &&
            t.execution.stdout === payload.stdout &&
            t.execution.stderr === payload.stderr &&
            t.execution.exit_code === payload.exit_code
          if (alreadyApplied) {
            return t
          }

          changed = true
          const stdout = truncateExecutionOutput(payload.stdout)
          const stderr = truncateExecutionOutput(payload.stderr)
          return ({
            ...t,
            // Do not hide pending approval on command completion unless we
            // received explicit exec_approval_resolved.
            phase: t.approval?.call_id ? 'awaiting_approval' : 'streaming',
            execution: {
              ...t.execution,
              status: 'completed',
              completed_at: Date.now(),
              stdout: stdout.text,
              stderr: stderr.text,
              stdout_truncated: stdout.truncated,
              stderr_truncated: stderr.truncated,
              exit_code: payload.exit_code
            }
          } as NodeTurn)
        })

        if (!changed) return

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

      if (type === 'worker_trace') {
        const swarm = findSwarmForPayload(payload)
        if (!swarm) return
        const nodeId = Number(payload.node_id)
        if (!Number.isFinite(nodeId)) return
        const eventName = String(payload.event ?? '')
        let message: string | null = null
        let level: NodeSystemEvent['level'] = 'info'
        if (eventName === 'codex_restart_requested') {
          message = `Codex session restart requested: ${String(payload.reason ?? 'unknown reason')}`
          level = 'warn'
        } else if (eventName === 'codex_restarted') {
          message = 'Codex session restarted'
        } else if (eventName === 'codex_rehydrate_sent') {
          message = 'Codex session rehydrated'
        }
        if (!message) return
        appendNodeSystemEvent(swarm, nodeId, {
          id: `${eventName}:${payload.restart_count ?? ''}:${Date.now()}`,
          ts: Date.now(),
          level,
          message
        })
        return
      }

      if (type === 'codex_stderr') {
        const swarm = findSwarmForPayload(payload)
        if (!swarm) return
        const nodeId = Number(payload.node_id)
        if (!Number.isFinite(nodeId)) return
        const line = String(payload.line ?? '')
        if (!line) return
        if (line.includes('failed to record rollout items: failed to queue rollout items: channel closed')) {
          appendNodeSystemEvent(swarm, nodeId, {
            id: `codex_stderr_rollout_closed:${Date.now()}`,
            ts: Date.now(),
            level: 'warn',
            message: 'Codex internal rollout channel closed'
          })
        }
        return
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
    projects: state.projects,
    selectedSwarm: state.selectedSwarm,
    selectedProject: state.selectedProject,
    activeNodeBySwarm: state.activeNodeBySwarm
  })
}))
