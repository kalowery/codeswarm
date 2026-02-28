import { create } from 'zustand'

export interface NodeTurn {
  injection_id: string
  deltas: string[]
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
  selectedSwarm?: string
  setSwarms: (swarms: any[]) => void
  addOrUpdateSwarm: (swarm: SwarmRecord) => void
  removeSwarm: (swarm_id: string) => void
  selectSwarm: (swarm_id: string) => void
  handleMessage: (msg: any) => void
}

export const useSwarmStore = create<SwarmStore>((set, get) => {
  // Track completions that arrive before turn_started
  const pendingComplete: Record<string, boolean> = {}

  return {
    swarms: {},
    selectedSwarm: undefined,

    setSwarms: (swarms) => {
      const map: Record<string, SwarmRecord> = {}

      swarms.forEach((s) => {
        const nodes: Record<number, NodeState> = {}
        for (let i = 0; i < s.node_count; i++) {
          nodes[i] = { node_id: i, turns: [] }
        }

        map[s.swarm_id] = {
          ...s,
          nodes
        }
      })

      set({ swarms: map })
    },

    addOrUpdateSwarm: (swarm) => {
      set((state) => ({
        swarms: { ...state.swarms, [swarm.swarm_id]: swarm }
      }))
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

      if (type === 'reconcile') {
        get().setSwarms(payload)
      }

      if (type === 'status') {
        const swarm = get().swarms[payload.swarm_id]
        if (!swarm) return

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

        const newTurn: NodeTurn = {
          injection_id: payload.injection_id,
          deltas: [],
          completed: false
        }

        if (pendingComplete[payload.injection_id]) {
          newTurn.completed = true
          delete pendingComplete[payload.injection_id]
        }

        turns.push(newTurn)

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
        if (!turn) return

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

      if (type === 'swarm_removed') {
        get().removeSwarm(payload.swarm_id)
      }
    }
  }
})
