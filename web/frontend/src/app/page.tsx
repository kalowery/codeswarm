'use client'

import { useEffect, useState, useRef } from 'react'
import { useSwarmStore } from '@/lib/store'
import { useWebSocket } from '@/lib/useWebSocket'
import LaunchModal from '@/components/LaunchModal'
import Image from 'next/image'

export default function Home() {
  const swarms = useSwarmStore((s) => s.swarms)
  const setSwarms = useSwarmStore((s) => s.setSwarms)
  const selectSwarm = useSwarmStore((s) => s.selectSwarm)
  const selected = useSwarmStore((s) => s.selectedSwarm)
  const setPendingPrompt = useSwarmStore((s) => s.setPendingPrompt)
  const launchError = useSwarmStore((s) => s.launchError)
  const clearLaunchError = useSwarmStore((s) => s.clearLaunchError)

  const { status: wsStatus } = useWebSocket()

  useEffect(() => {
    const apiBase = `${window.location.protocol}//${window.location.hostname}:4000`
    fetch(`${apiBase}/swarms`)
      .then((res) => res.json())
      .then((data) => setSwarms(data))
  }, [setSwarms])

  const pendingLaunches = useSwarmStore((s) => s.pendingLaunches)
  const swarmList = Object.values(swarms)
  const active = selected ? swarms[selected] : undefined

  function nodeNeedsAttention(swarmId: string, nodeId: number) {
    const swarm = swarms[swarmId]
    if (!swarm) return false

    const node = swarm.nodes[nodeId]
    if (!node || node.turns.length === 0) return false

    const last = node.turns[node.turns.length - 1]
    if (!last.completed) return false

    const activeNode = useSwarmStore.getState().activeNodeBySwarm[swarmId] ?? 0
    const isActive = swarmId === selected && nodeId === activeNode

    return !isActive
  }

  function swarmNeedsAttention(swarmId: string) {
    const swarm = swarms[swarmId]
    if (!swarm) return false

    return Object.keys(swarm.nodes).some((id) =>
      nodeNeedsAttention(swarmId, Number(id))
    )
  }

  const [showLaunch, setShowLaunch] = useState(false)
  const nodeScrollRef = useRef<HTMLDivElement | null>(null)
  const [canScrollLeft, setCanScrollLeft] = useState(false)
  const [canScrollRight, setCanScrollRight] = useState(false)

  function updateScrollButtons() {
    const el = nodeScrollRef.current
    if (!el) return
    setCanScrollLeft(el.scrollLeft > 0)
    setCanScrollRight(el.scrollLeft + el.clientWidth < el.scrollWidth - 1)
  }

  useEffect(() => {
    function handleResize() {
      updateScrollButtons()
    }

    window.addEventListener('resize', handleResize)
    // Run once after mount / active change
    setTimeout(updateScrollButtons, 0)

    return () => window.removeEventListener('resize', handleResize)
  }, [active])
  const [isSending, setIsSending] = useState(false)
  const [isTerminating, setIsTerminating] = useState(false)
  const [expandedReasoning, setExpandedReasoning] = useState<Record<string, boolean>>({})
  const [dotCount, setDotCount] = useState(0)

  useEffect(() => {
    const id = setInterval(() => {
      setDotCount((d) => (d + 1) % 4)
    }, 500)
    return () => clearInterval(id)
  }, [])

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100 flex overflow-x-hidden">
      {/* Sidebar */}
      <div className="w-80 shrink-0 border-r border-slate-800 p-4">
        <div className="mb-4 space-y-3">

          {/* Logo + Title */}
          <div className="flex items-center gap-3">
            <Image
              src="/codeswarm.jpg"
              alt="Codeswarm"
              width={32}
              height={32}
              className="rounded-lg ring-1 ring-slate-700"
            />
            <div>
              <div className="text-lg font-semibold tracking-wide">
                Codeswarm
              </div>
              <div className="text-xs text-slate-500 -mt-1">
                Swarm Control
              </div>
            </div>
          </div>

          {/* Status + Launch */}
          <div className="flex items-center justify-between">
            <span className={`text-xs px-2 py-0.5 rounded border ${
              wsStatus === 'connected'
                ? 'bg-emerald-900 border-emerald-500 text-emerald-400'
                : wsStatus === 'reconnecting' || wsStatus === 'connecting'
                ? 'bg-amber-900 border-amber-500 text-amber-400'
                : 'bg-rose-900 border-rose-500 text-rose-400'
            }`}>
              WS: {wsStatus}
            </span>

            <button
              onClick={() => setShowLaunch(true)}
              className="px-2 py-1 bg-indigo-600 rounded text-sm"
            >
              + Launch
            </button>
          </div>
        </div>
        <div className="space-y-2">
          {/* Launch Error Banner */}
          {launchError && (
            <div className="p-3 rounded border bg-rose-900 border-rose-500 text-rose-200 text-sm">
              <div className="flex justify-between items-start gap-2">
                <div className="flex-1 min-w-0">
                  ⚠ Launch failed:
                  <div className="mt-1 text-xs whitespace-pre-wrap break-words max-h-40 overflow-y-auto">
                    {launchError}
                  </div>
                </div>
                <button
                  onClick={clearLaunchError}
                  className="text-xs text-rose-300 hover:underline"
                >
                  Dismiss
                </button>
              </div>
            </div>
          )}

          {/* Pending Launch Ghosts */}
          {Object.entries(pendingLaunches).map(([reqId, launch]) => (
            <div
              key={reqId}
              className="p-3 rounded border bg-slate-900 border-amber-500"
            >
              <div className="font-medium flex items-center gap-2">
                {launch.alias}
                <span className="inline-block w-2 h-2 rounded-full bg-amber-400 animate-pulse" />
              </div>
              <div className="text-sm text-amber-400">
                LAUNCHING...
              </div>
            </div>
          ))}

          {swarmList.map((swarm) => (
            <div
              key={swarm.swarm_id}
              onClick={() => selectSwarm(swarm.swarm_id)}
              className={`p-3 rounded cursor-pointer border transition relative ${
                selected === swarm.swarm_id
                  ? 'bg-slate-800 border-indigo-500'
                  : 'bg-slate-900 border-slate-800 hover:bg-slate-800'
              }`}
            >
              {swarmNeedsAttention(swarm.swarm_id) && (
                <span className="absolute top-2 right-2 w-2 h-2 rounded-full bg-amber-400 animate-pulse" />
              )}
              <div className="font-medium">{swarm.alias}</div>
              <div className="text-sm text-slate-400">
                {(swarm.status ?? 'unknown').toUpperCase()} · {swarm.node_count} node
              </div>
            </div>
          ))}
          {swarmList.length === 0 && (
            <div className="text-slate-500 text-sm">No active swarms</div>
          )}
        </div>
      </div>

      {/* Detail Panel */}
      <div className="flex-1 min-w-0 p-6">
        {!active && (
          <div className="text-slate-500">Select a swarm to view details</div>
        )}

        {active && (
          <div>
            <div className="flex items-center justify-between mb-4">
              <div>
                <h1 className="text-xl font-semibold">{active.alias}</h1>
                <div className="text-sm text-slate-400">
                  Status: {active.status ? active.status : 'unknown'} · Slurm: {active.slurm_state}
                </div>
              </div>
              <button
                disabled={isTerminating}
                onClick={async () => {
                  if (isTerminating) return
                  if (!confirm(`Terminate ${active.alias}? This cannot be undone.`)) return

                  try {
                    setIsTerminating(true)
                    const apiBase = `${window.location.protocol}//${window.location.hostname}:4000`
                    await fetch(`${apiBase}/terminate/${active.alias}`, {
                      method: 'POST'
                    })
                  } finally {
                    setIsTerminating(false)
                  }
                }}
                className={`px-3 py-1 rounded text-sm ${
                  isTerminating
                    ? 'bg-slate-700 text-slate-400 cursor-not-allowed'
                    : 'bg-rose-600 hover:bg-rose-500'
                }`}
              >
                {isTerminating ? 'Terminating…' : 'Terminate'}
              </button>
            </div>

            <div className="bg-slate-900 border border-slate-800 rounded p-4 h-[400px] overflow-y-auto overflow-x-hidden text-sm space-y-4">
              {/* Node Tabs */}
              {(() => {
                const activeNodeBySwarm = useSwarmStore.getState().activeNodeBySwarm
                const setActiveNode = useSwarmStore.getState().setActiveNode
                const activeNodeId = activeNodeBySwarm[active.swarm_id] ?? 0
                const activeNode = active.nodes[activeNodeId]

                return (
                  <>
                    <div className="mb-3 border-b border-slate-800 pb-3 relative">
                      <div className="text-xs text-slate-500 mb-2">
                        Nodes ({Object.keys(active.nodes).length})
                      </div>

                      {canScrollLeft && (
                        <button
                          onClick={() => {
                            nodeScrollRef.current?.scrollBy({ left: -200, behavior: 'smooth' })
                          }}
                          className="absolute left-0 top-8 z-10 px-2 py-1 bg-slate-900 border border-slate-700 rounded"
                        >
                          ◀
                        </button>
                      )}

                      {canScrollRight && (
                        <button
                          onClick={() => {
                            nodeScrollRef.current?.scrollBy({ left: 200, behavior: 'smooth' })
                          }}
                          className="absolute right-0 top-8 z-10 px-2 py-1 bg-slate-900 border border-slate-700 rounded"
                        >
                          ▶
                        </button>
                      )}

                      <div
                        ref={nodeScrollRef}
                        onScroll={updateScrollButtons}
                        className="flex flex-nowrap gap-2 overflow-x-auto pr-8 pl-8 w-full"
                      >
                        {Object.keys(active.nodes).map((nodeId) => {
                          const id = Number(nodeId)
                          const isActive = id === activeNodeId
                          const needsAttention = nodeNeedsAttention(active.swarm_id, id)
                          const node = active.nodes[id]
                          const lastTurn = node.turns[node.turns.length - 1]
                          const isWorking = lastTurn && !lastTurn.completed

                          return (
                            <button
                              key={nodeId}
                              onClick={() => setActiveNode(active.swarm_id, id)}
                              className={`relative min-w-[72px] shrink-0 px-3 py-2 text-xs rounded-t-md transition border-b-2 ${
                                isActive
                                  ? 'bg-slate-800 text-white border-indigo-500'
                                  : 'bg-slate-900 text-slate-400 border-slate-700 hover:bg-slate-800'
                              }`}
                            >
                              {needsAttention && (
                                <span className="absolute top-1 right-1 w-2 h-2 rounded-full bg-amber-400 animate-pulse" />
                              )}
                              {isWorking && !isActive && (
                                <span className="absolute bottom-1 left-1 w-2 h-2 rounded-full bg-emerald-400 animate-pulse" />
                              )}
                              {id}
                            </button>
                          )
                        })}
                      </div>
                    </div>

                    <div className="space-y-4">
                      {activeNode.turns.map((turn, idx) => (
                        <div key={idx} className="space-y-2">
                      {/* User message */}
                      {turn.prompt && (
                        <div className="flex justify-end">
                          <div className="max-w-[75%] bg-indigo-600 text-white px-3 py-2 rounded-lg rounded-br-sm">
                            {turn.prompt}
                          </div>
                        </div>
                      )}

                      {/* Assistant + Execution Block */}
                      <div className="flex justify-start">
                        <div className="max-w-[75%] bg-slate-800 border border-slate-700 px-3 py-2 rounded-lg rounded-bl-sm space-y-2">

                          {/* Activity Indicator */}
                          {!turn.completed && (
                            <div className="flex items-center gap-2 text-[10px] text-emerald-400">
                              <span className="inline-block w-2 h-2 rounded-full bg-emerald-500 animate-pulse" />
                              <span>Working{'.'.repeat(dotCount)}</span>
                            </div>
                          )}

                          {/* Collapsible Reasoning with Live Preview */}
                          {(!turn.completed || turn.reasoning.length > 0) && (
                            <div className="text-xs">
                              <button
                                className="text-amber-400 hover:underline"
                                onClick={() =>
                                  setExpandedReasoning(prev => ({
                                    ...prev,
                                    [turn.injection_id]: !prev[turn.injection_id]
                                  }))
                                }
                              >
                                🧠 {expandedReasoning[turn.injection_id]
                                  ? 'Hide reasoning'
                                  : (() => {
                                      if (!turn.reasoning && !turn.completed) {
                                        return 'Thinking' + '.'.repeat(dotCount)
                                      }
                                      const preview = turn.reasoning.slice(0, 60)
                                      return turn.completed
                                        ? preview
                                        : preview + '.'.repeat(dotCount)
                                    })()
                                }
                              </button>

                              {expandedReasoning[turn.injection_id] && turn.reasoning && (
                                <div className="mt-1 text-amber-400 whitespace-pre-wrap">
                                  {turn.reasoning}
                                </div>
                              )}
                            </div>
                          )}

                          {/* Command executions */}
                          {turn.commands.map((cmd, i) => (
                            <div key={i} className="text-xs bg-slate-900 border border-slate-700 rounded p-2">
                              <div className="text-slate-400">
                                $ {Array.isArray(cmd.command) ? cmd.command.join(' ') : cmd.command}
                              </div>
                              {cmd.status === 'completed' && (
                                <div className="mt-1 text-emerald-400 whitespace-pre-wrap">
                                  {cmd.stdout}
                                </div>
                              )}
                            </div>
                          ))}

                          {/* Assistant output */}
                          {turn.deltas.length > 0 ? (
                            <>
                              <div className="whitespace-pre-wrap">
                                {turn.deltas.join('')}
                              </div>
                              {!turn.completed && (
                                <span className="animate-pulse">▌</span>
                              )}
                            </>
                          ) : turn.completed ? (
                            <span className="text-slate-400 italic">
                              ✓ Completed (no user-visible output)
                            </span>
                          ) : (
                            <span className="animate-pulse">▌</span>
                          )}

                          {/* Error */}
                          {turn.error && (
                            <div className="text-rose-400 text-xs">
                              ⚠ {turn.error}
                            </div>
                          )}

                          {/* Usage */}
                          {turn.usage && (
                            <div className="text-[10px] text-slate-500">
                              Tokens: {turn.usage}
                            </div>
                          )}
                        </div>
                      </div>
                    </div>
                      ))}
                    </div>
                  </>
                )
              })()}
            </div>

            {/* Inject Box */}
            <div className="mt-4">
              <textarea
                placeholder="Enter prompt..."
                disabled={isSending}
                className={`w-full border rounded px-3 py-2 h-20 ${
                  isSending
                    ? 'bg-slate-700 border-slate-600 text-slate-400 cursor-not-allowed'
                    : 'bg-slate-800 border-slate-700'
                }`}
                onKeyDown={async (e) => {
                  if (e.key === 'Enter' && !e.shiftKey) {
                    e.preventDefault()
                    if (isSending) return

                    const value = (e.target as HTMLTextAreaElement).value
                    if (!value.trim()) return

                    try {
                      setIsSending(true)
                      setPendingPrompt(value)

                      // Optimistically add provisional turn
                      const store = useSwarmStore.getState()
                      const swarm = store.swarms[active.swarm_id]
                      const activeNodeId = store.activeNodeBySwarm[active.swarm_id] ?? 0

                      function parseInput(input: string) {
                        const trimmed = input.trim()

                        if (trimmed.startsWith('/all ')) {
                          return {
                            targets: Object.keys(swarm.nodes).map(n => Number(n)),
                            content: trimmed.slice(5).trim()
                          }
                        }

                        const match = trimmed.match(/^\/node\[(\d+)\]\s+(.*)/)
                        if (match) {
                          const nodeId = Number(match[1])
                          return {
                            targets: [nodeId],
                            content: match[2]
                          }
                        }

                        return {
                          targets: [activeNodeId],
                          content: input
                        }
                      }

                      const { targets, content } = parseInput(value)

                      const updatedNodes = { ...swarm.nodes }

                      for (const target of targets) {
                        const node = updatedNodes[target]
                        const provisional = {
                          injection_id: `temp-${Date.now()}-${target}`,
                          prompt: content,
                          deltas: [],
                          reasoning: '',
                          commands: [],
                          completed: false
                        }

                        updatedNodes[target] = {
                          ...node,
                          turns: [...node.turns, provisional]
                        }
                      }

                      store.addOrUpdateSwarm({
                        ...swarm,
                        nodes: updatedNodes
                      })

                      const apiBase = `${window.location.protocol}//${window.location.hostname}:4000`
                      await fetch(`${apiBase}/inject/${active.alias}`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ prompt: content, nodes: targets })
                      })

                      ;(e.target as HTMLTextAreaElement).value = ''
                    } finally {
                      setTimeout(() => setIsSending(false), 300)
                    }
                  }
                }}
              />
              <div className="text-xs text-slate-500 mt-1">
                Press Enter to send (Shift+Enter for newline)
              </div>
            </div>
          </div>
        )}
      </div>
      {showLaunch && (
        <LaunchModal onClose={() => setShowLaunch(false)} />
      )}
    </div>
  )
}
