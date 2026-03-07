import { create } from 'zustand'

const MAX_TURNS_PER_NODE = 300
const STREAM_IDLE_COMPLETE_MS = 1500

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

export const useSwarmStore = create<SwarmStore>((set, get) => {
  // Track completions that arrive before turn_started
  const pendingComplete: Record<string, boolean> = {} // deprecated
  const pendingReasoningDeltas: Record<string, string[]> = {}
  const pendingReasoning: Record<string, string> = {}
  const pendingTaskComplete: Record<string, { content?: string }> = {}
  const idleCompletionTimers: Record<string, ReturnType<typeof setTimeout>> = {}

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

          if (existing) {
            // Preserve existing nodes and turns, update metadata
            updated[s.swarm_id] = {
              ...existing,
              ...s,
              known_exec_policies:
                s.known_exec_policies ?? existing.known_exec_policies ?? [],
              pending_approvals:
                s.pending_approvals ?? existing.pending_approvals ?? {},
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
              pending_approvals: s.pending_approvals ?? {},
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
          pending_approvals: removePendingApprovalByInjectionId(
            swarm.pending_approvals,
            payload.injection_id,
            nodeId
          ),
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
          pending_approvals: removePendingApprovalByInjectionId(
            swarm.pending_approvals,
            payload.injection_id,
            nodeId
          ),
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
          pending_approvals: removePendingApprovalByInjectionId(
            swarm.pending_approvals,
            payload.injection_id,
            nodeId
          ),
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
        const hadAnyPendingApproval = Object.values(state.swarms).some((s) => {
          const pending = s.pending_approvals ?? {}
          if (Object.values(pending).some((arr) => (arr?.length ?? 0) > 0)) return true
          return Object.values(s.nodes).some((n) =>
            n.turns.some((t) => t.phase === 'awaiting_approval' && !!t.approval)
          )
        })
        let swarm: SwarmRecord | undefined = state.swarms[payload.swarm_id]
        if (!swarm && typeof payload?.job_id === 'string') {
          swarm = Object.values(state.swarms).find((s) => s.job_id === payload.job_id)
        }
        if (!swarm && typeof payload?.call_id === 'string' && payload.call_id) {
          swarm = Object.values(state.swarms).find((s) =>
            Object.values(s.nodes).some((n) =>
              n.turns.some((t) => t.approval?.call_id === payload.call_id)
            )
          )
        }
        if (!swarm) return

        const upsertPendingApproval = (targetSwarm: SwarmRecord, nodeId: number) => {
          const current = targetSwarm.pending_approvals ?? {}
          const existing = current[nodeId] ?? []
          const callId = String(payload.call_id ?? '')
          if (!callId) return current
          const existingEntry = existing.find((a) => a.call_id === callId)
          const approval = {
            call_id: callId,
            injection_id:
              typeof payload?.injection_id === 'string' ? payload.injection_id : undefined,
            created_at_ms: existingEntry?.created_at_ms ?? Date.now(),
            command: payload.command,
            reason: payload.reason,
            cwd: payload.cwd,
            proposed_execpolicy_amendment: payload.proposed_execpolicy_amendment,
            available_decisions: payload.available_decisions
          } as PendingApproval
          const idx = existing.findIndex((a) => a.call_id === callId)
          const nextNodeApprovals =
            idx >= 0
              ? existing.map((a, i) => (i === idx ? approval : a))
              : [...existing, approval]
          return {
            ...current,
            [nodeId]: nextNodeApprovals
          }
        }

        // Duplicate approval events for the same call_id can arrive from
        // multiple protocol shapes. If we've already materialized this call_id
        // on any node, update in place without reordering turns.
        if (typeof payload?.call_id === 'string' && payload.call_id) {
          const eventNodeId = Number(payload.node_id)
          let existingByCall: [string, NodeState] | undefined
          if (Number.isFinite(eventNodeId)) {
            const eventNode = swarm.nodes[eventNodeId]
            if (
              eventNode &&
              eventNode.turns.some((t) => t.approval?.call_id === payload.call_id)
            ) {
              existingByCall = [String(eventNodeId), eventNode]
            }
          }
          if (!existingByCall) {
            existingByCall = Object.entries(swarm.nodes).find(([, n]) =>
              n.turns.some((t) => t.approval?.call_id === payload.call_id)
            ) as [string, NodeState] | undefined
          }
          if (existingByCall) {
            const existingNodeId = Number(existingByCall[0])
            const existingNode = swarm.nodes[existingNodeId]
            const updatedTurns = existingNode.turns.map((t) =>
              t.approval?.call_id === payload.call_id
                ? ({
                    ...t,
                    phase: 'awaiting_approval',
                    approval: {
                      call_id: payload.call_id,
                      command: payload.command,
                      reason: payload.reason,
                      cwd: payload.cwd,
                      proposed_execpolicy_amendment: payload.proposed_execpolicy_amendment,
                      available_decisions: payload.available_decisions
                    }
                  } as NodeTurn)
                : t
            )

            get().addOrUpdateSwarm({
              ...swarm,
              pending_approvals: upsertPendingApproval(swarm, existingNodeId),
              nodes: {
                ...swarm.nodes,
                [existingNodeId]: { ...existingNode, turns: capTurns(updatedTurns) }
              }
            })
            if (!hadAnyPendingApproval) {
              get().selectSwarm(swarm.swarm_id)
              get().setActiveNode(swarm.swarm_id, existingNodeId)
            }
            clearIdleCompletionTimer(
              swarm.swarm_id,
              existingNodeId,
              typeof payload.injection_id === 'string' ? payload.injection_id : ''
            )
            return
          }
        }

        let resolvedNodeId = Number(payload.node_id)
        const byCallAnywhere =
          typeof payload?.call_id === 'string' && payload.call_id
            ? Object.entries(swarm.nodes).find(([, n]) =>
                n.turns.some((t) => t.approval?.call_id === payload.call_id)
              )
            : undefined
        if (byCallAnywhere) {
          resolvedNodeId = Number(byCallAnywhere[0])
        }
        if (!Number.isFinite(resolvedNodeId)) {
          const byInjection = typeof payload?.injection_id === 'string' && payload.injection_id
            ? Object.entries(swarm.nodes).find(([, n]) =>
                n.turns.some((t) => t.injection_id === payload.injection_id)
              )
            : undefined
          if (byInjection) {
            resolvedNodeId = Number(byInjection[0])
          }
        }
        if (!Number.isFinite(resolvedNodeId)) {
          const byCall = typeof payload?.call_id === 'string' && payload.call_id
            ? Object.entries(swarm.nodes).find(([, n]) =>
                n.turns.some((t) => t.approval?.call_id === payload.call_id)
              )
            : undefined
          if (byCall) {
            resolvedNodeId = Number(byCall[0])
          }
        }
        if (!Number.isFinite(resolvedNodeId)) {
          resolvedNodeId = state.activeNodeBySwarm[swarm.swarm_id] ?? 0
        }

        const nodeId = resolvedNodeId
        const node = swarm.nodes[nodeId]
        if (!node) return
        const approvalState = {
          call_id: payload.call_id,
          command: payload.command,
          reason: payload.reason,
          cwd: payload.cwd,
          proposed_execpolicy_amendment: payload.proposed_execpolicy_amendment,
          available_decisions: payload.available_decisions
        }

        const targetIdx = node.turns.findIndex((t) => t.injection_id === payload.injection_id)
        const fallbackIdx =
          targetIdx >= 0
            ? targetIdx
            : node.turns.findIndex((t) => t.approval?.call_id === payload.call_id)

        let updatedTurns: NodeTurn[]
        let approvalTurn: NodeTurn | undefined
        if (fallbackIdx >= 0) {
          const mapped = node.turns.map((t, idx) =>
            idx === fallbackIdx
              ? ({
                  ...t,
                  phase: 'awaiting_approval',
                  approval: approvalState
                } as NodeTurn)
              : t
          )
          approvalTurn = mapped[fallbackIdx]
          updatedTurns = mapped.filter((_, idx) => idx !== fallbackIdx)
        } else {
          // If approval arrives before a matching turn is visible, append a
          // synthetic turn so the dialog is never lost off-state.
          approvalTurn = {
            injection_id:
              typeof payload.injection_id === 'string' && payload.injection_id
                ? payload.injection_id
                : `approval-${payload.call_id}`,
            prompt: '',
            deltas: [],
            reasoning: '',
            phase: 'awaiting_approval',
            approval: approvalState
          } as NodeTurn
          updatedTurns = [...node.turns]
        }

        if (approvalTurn) {
          // Keep pending approval at the tail so it is visible in active transcript
          // even when the original turn index is older in history.
          updatedTurns = [...updatedTurns, approvalTurn]
        }

        get().addOrUpdateSwarm({
          ...swarm,
          pending_approvals: upsertPendingApproval(swarm, nodeId),
          nodes: {
            ...swarm.nodes,
            [nodeId]: { ...node, turns: capTurns(updatedTurns) }
          }
        })
        if (!hadAnyPendingApproval) {
          get().selectSwarm(swarm.swarm_id)
          // Ensure keyboard prompt routing and tab selection follow first pending approval target.
          get().setActiveNode(swarm.swarm_id, nodeId)
        }
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

        const targetNodeKeys = hasResolvedNode
          ? [String(resolvedNodeId)]
          : Object.keys(updatedNodes)
        for (const nodeKey of targetNodeKeys) {
          const node = updatedNodes[Number(nodeKey)]
          if (!node) continue

          updatedNodes[Number(nodeKey)] = {
            ...node,
            turns: node.turns.map((t) => {
              const isMatch =
                t.approval?.call_id === payload.call_id &&
                (!payload.injection_id || t.injection_id === payload.injection_id)
              return isMatch
                ? ({
                    ...t,
                    phase: 'streaming',
                    approval: undefined
                  } as NodeTurn)
                : t
            })
          }
        }

        const updatedPendingApprovals: Record<number, PendingApproval[]> = {}
        const pendingApprovals = swarm.pending_approvals ?? {}
        for (const [nodeKey, approvals] of Object.entries(pendingApprovals)) {
          const nodeNum = Number(nodeKey)
          if (hasResolvedNode && nodeNum !== resolvedNodeId) {
            if ((approvals ?? []).length > 0) {
              updatedPendingApprovals[nodeNum] = approvals ?? []
            }
            continue
          }
          const filtered = (approvals ?? []).filter((a) => {
            if (a.call_id !== payload.call_id) return true
            if (!payload.injection_id) return false
            return a.injection_id !== payload.injection_id
          })
          if (filtered.length > 0) {
            updatedPendingApprovals[nodeNum] = filtered
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
})
