import { create } from 'zustand'

const MAX_TURNS_PER_NODE = 300

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

export interface NodeTurn {
  injection_id: string
  prompt: string
  deltas: string[]
  reasoning: string
  phase: TurnPhase
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
  usage?: number
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
  known_exec_policies?: string[][]
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

interface SwarmStore {
  swarms: Record<string, SwarmRecord>
  interSwarmQueue: InterSwarmQueueItem[]
  pendingLaunches: Record<string, { alias: string }>
  selectedSwarm?: string
  pendingPrompt?: string
  launchError: string | null
  activeNodeBySwarm: Record<string, number>
  setActiveNode: (swarm_id: string, node_id: number) => void
  setPendingPrompt: (prompt: string) => void
  setLaunchError: (message: string) => void
  clearLaunchError: () => void
  addPendingLaunch: (request_id: string, alias: string) => void
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
        pendingLaunches: {
          ...state.pendingLaunches,
          [request_id]: { alias }
        }
      })),
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

        // Fallback for missing turn_complete/task_complete events: when a thread
        // is idle, ensure no node stays in a perpetual working state.
        if (payload.status?.type !== 'idle') return

        const nodeId = Number(payload.node_id)
        const node = swarm.nodes[nodeId]
        if (!node) return

        const updatedTurns = node.turns.map((t) =>
          t.phase === 'streaming' || t.phase === 'executing'
            ? ({ ...t, phase: 'completed' } as NodeTurn)
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
          phase: 'streaming'
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

        if (turn.deltas.length === 0) {
          turn.deltas = [payload.content]
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

        get().addOrUpdateSwarm({
          ...swarm,
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
            // Keep streamed content as source of truth. task_complete payload can
            // be cumulative and may include prior turn text.
            deltas:
              existing.deltas.length > 0
                ? existing.deltas
                : content
                ? [content]
                : existing.deltas
          } as NodeTurn)
        } else {
          // Do not overwrite provisional temp turns with mismatched injection IDs.
          // Late task_complete from a prior turn can otherwise pollute the current
          // working bubble. Cache until the matching turn_started arrives.
          pendingTaskComplete[payload.injection_id] = { content }
          return
        }

        get().addOrUpdateSwarm({
          ...swarm,
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
        const swarm = get().swarms[payload.swarm_id]
        if (!swarm) return
        const nodeId = Number(payload.node_id)
        const node = swarm.nodes[nodeId]
        if (!node) return

        const updatedTurns: NodeTurn[] = node.turns.map((t) =>
          t.injection_id === payload.injection_id
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
          nodes: {
            ...swarm.nodes,
            [nodeId]: { ...node, turns: capTurns(updatedTurns) }
          }
        })
      }

      if (type === 'exec_approval_resolved') {
        const swarm = Object.values(get().swarms).find(
          (s) => s.job_id === payload.job_id
        )
        if (!swarm) return

        const updatedNodes = { ...swarm.nodes }
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

        for (const nodeKey of Object.keys(updatedNodes)) {
          const node = updatedNodes[Number(nodeKey)]

          updatedNodes[Number(nodeKey)] = {
            ...node,
            turns: node.turns.map((t) =>
              t.approval?.call_id === payload.call_id
                ? ({
                    ...t,
                    phase: 'streaming',
                    approval: undefined
                  } as NodeTurn)
                : t
            )
          }
        }

        get().addOrUpdateSwarm({
          ...swarm,
          known_exec_policies: nextPolicies,
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
                phase: 'executing',
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
          nodes: {
            ...swarm.nodes,
            [nodeId]: { ...node, turns: updatedTurns }
          }
        })
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
            phase: 'streaming',
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
      }

      if (type === 'usage') {
        const swarm = get().swarms[payload.swarm_id]
        if (!swarm) return
        const nodeId = Number(payload.node_id)
        const node = swarm.nodes[nodeId]
        if (!node) return

        const updatedTurns: NodeTurn[] = node.turns.map((t) =>
          t.injection_id === payload.injection_id
            ? ({ ...t, usage: payload.total_tokens } as NodeTurn)
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
