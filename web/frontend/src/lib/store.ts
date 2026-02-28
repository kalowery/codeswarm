import { create } from 'zustand'

export interface CommandExecution {
  call_id: string
  command: string[] | string
  cwd?: string
  stdout?: string
  stderr?: string
  exit_code?: number
  status: 'started' | 'completed'
}

export interface NodeTurn {
  injection_id: string
  prompt: string
  deltas: string[]
  reasoning: string
  commands: CommandExecution[]
  error?: string
  usage?: number
  completed: boolean
}

export interface NodeState {
  node_id: number
  turns: NodeTurn[]
}

export interface SwarmRecord {
  swarm_id: string
  alias: string
  job_id: string
  node_count: number
  status: string
  slurm_state?: string
  nodes: Record<number, NodeState>
}

interface SwarmStore {
  swarms: Record<string, SwarmRecord>
  pendingLaunches: Record<string, { alias: string }>
  selectedSwarm?: string
  pendingPrompt?: string
  launchError: string | null
  setPendingPrompt: (prompt: string) => void
  setLaunchError: (message: string) => void
  clearLaunchError: () => void
  addPendingLaunch: (request_id: string, alias: string) => void
  removePendingLaunch: (request_id: string) => void
  setSwarms: (swarms: any[]) => void
  addOrUpdateSwarm: (swarm: SwarmRecord) => void
  removeSwarm: (swarm_id: string) => void
  selectSwarm: (swarm_id: string) => void
  handleMessage: (msg: any) => void
}

export const useSwarmStore = create<SwarmStore>((set, get) => {
  // Track completions that arrive before turn_started
  const pendingComplete: Record<string, boolean> = {}
  const pendingDeltas: Record<string, string[]> = {}
  const pendingAssistant: Record<string, string> = {}

  return {
    swarms: {},
    pendingLaunches: {},
    selectedSwarm: undefined,
    pendingPrompt: undefined,
    launchError: null,
    setPendingPrompt: (prompt: string) => set({ pendingPrompt: prompt }),
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
              ...swarm,
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

      if (type === 'turn_started') {
        const swarm = get().swarms[payload.swarm_id]
        if (!swarm) return

        const nodeId = Number(payload.node_id)
        const node = swarm.nodes[nodeId]
        if (!node) return

        const turns = [...node.turns]
        const prompt = get().pendingPrompt || ''

        // Check for provisional optimistic turn
        const provisionalIndex = turns.findIndex(
          (t) => t.injection_id.startsWith('temp-') && !t.completed
        )

        const newTurn: NodeTurn = {
          injection_id: payload.injection_id,
          prompt,
          deltas: [],
          reasoning: '',
          commands: [],
          completed: false
        }

        if (pendingDeltas[payload.injection_id]) {
          newTurn.deltas = [...pendingDeltas[payload.injection_id]]
          delete pendingDeltas[payload.injection_id]
        }

        if (pendingAssistant[payload.injection_id] && newTurn.deltas.length === 0) {
          newTurn.deltas = [pendingAssistant[payload.injection_id]]
          delete pendingAssistant[payload.injection_id]
        }

        if (pendingComplete[payload.injection_id]) {
          newTurn.completed = true
          delete pendingComplete[payload.injection_id]
        }

        if (provisionalIndex !== -1) {
          // Replace provisional turn with real injection_id
          turns[provisionalIndex] = newTurn
        } else {
          turns.push(newTurn)
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
          if (!pendingDeltas[payload.injection_id]) {
            pendingDeltas[payload.injection_id] = []
          }
          pendingDeltas[payload.injection_id].push(payload.content)
          return
        }

        turn.deltas = [...turn.deltas, payload.content]

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
          pendingAssistant[payload.injection_id] = payload.content
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
              turns
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
          pendingComplete[payload.injection_id] = true
          return
        }

        turn.completed = true

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

      if (type === 'reasoning_delta') {
        const swarm = get().swarms[payload.swarm_id]
        if (!swarm) return
        const nodeId = Number(payload.node_id)
        const node = swarm.nodes[nodeId]
        if (!node) return

        const updatedTurns = node.turns.map((t) =>
          t.injection_id === payload.injection_id
            ? { ...t, reasoning: (t.reasoning ?? '') + payload.content }
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

      if (type === 'reasoning') {
        const swarm = get().swarms[payload.swarm_id]
        if (!swarm) return
        const nodeId = Number(payload.node_id)
        const node = swarm.nodes[nodeId]
        if (!node) return

        const updatedTurns = node.turns.map((t) =>
          t.injection_id === payload.injection_id
            ? { ...t, reasoning: payload.content }
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

      if (type === 'command_started') {
        const swarm = get().swarms[payload.swarm_id]
        if (!swarm) return
        const nodeId = Number(payload.node_id)
        const node = swarm.nodes[nodeId]
        if (!node) return

        const updatedTurns = node.turns.map((t) =>
          t.injection_id === payload.injection_id
            ? {
                ...t,
                commands: [
                  ...t.commands,
                  {
                    call_id: payload.call_id,
                    command: payload.command,
                    cwd: payload.cwd,
                    status: 'started'
                  }
                ]
              }
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

        const updatedTurns = node.turns.map((t) => {
          if (t.injection_id !== payload.injection_id) return t
          return {
            ...t,
            commands: t.commands.map((c) =>
              c.call_id === payload.call_id
                ? {
                    ...c,
                    status: 'completed',
                    stdout: payload.stdout,
                    stderr: payload.stderr,
                    exit_code: payload.exit_code
                  }
                : c
            )
          }
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

        const updatedTurns = node.turns.map((t) =>
          t.injection_id === payload.injection_id
            ? { ...t, error: payload.message }
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

        const updatedTurns = node.turns.map((t) =>
          t.injection_id === payload.injection_id
            ? { ...t, usage: payload.total_tokens }
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
