'use client'

import { useEffect, useState } from 'react'
import { useSwarmStore } from '@/lib/store'
import { useWebSocket } from '@/lib/useWebSocket'
import LaunchModal from '@/components/LaunchModal'

export default function Home() {
  const swarms = useSwarmStore((s) => s.swarms)
  const setSwarms = useSwarmStore((s) => s.setSwarms)
  const selectSwarm = useSwarmStore((s) => s.selectSwarm)
  const selected = useSwarmStore((s) => s.selectedSwarm)
  const setPendingPrompt = useSwarmStore((s) => s.setPendingPrompt)

  const { status: wsStatus } = useWebSocket()

  useEffect(() => {
    const apiBase = `${window.location.protocol}//${window.location.hostname}:4000`
    fetch(`${apiBase}/swarms`)
      .then((res) => res.json())
      .then((data) => setSwarms(data))
  }, [setSwarms])

  const swarmList = Object.values(swarms)
  const active = selected ? swarms[selected] : undefined
  const [showLaunch, setShowLaunch] = useState(false)
  const [isSending, setIsSending] = useState(false)
  const [isTerminating, setIsTerminating] = useState(false)
  const [expandedReasoning, setExpandedReasoning] = useState<Record<string, boolean>>({})

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100 flex">
      {/* Sidebar */}
      <div className="w-80 border-r border-slate-800 p-4">
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-3">
            <h2 className="text-lg font-semibold">Swarms</h2>
            <span className={`text-xs px-2 py-0.5 rounded border ${
              wsStatus === 'connected'
                ? 'bg-emerald-900 border-emerald-500 text-emerald-400'
                : wsStatus === 'reconnecting' || wsStatus === 'connecting'
                ? 'bg-amber-900 border-amber-500 text-amber-400'
                : 'bg-rose-900 border-rose-500 text-rose-400'
            }`}>
              WS: {wsStatus}
            </span>
          </div>
          <button
            onClick={() => setShowLaunch(true)}
            className="px-2 py-1 bg-indigo-600 rounded text-sm"
          >
            + Launch
          </button>
        </div>
        <div className="space-y-2">
          {swarmList.map((swarm) => (
            <div
              key={swarm.swarm_id}
              onClick={() => selectSwarm(swarm.swarm_id)}
              className={`p-3 rounded cursor-pointer border transition ${
                selected === swarm.swarm_id
                  ? 'bg-slate-800 border-indigo-500'
                  : 'bg-slate-900 border-slate-800 hover:bg-slate-800'
              }`}
            >
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
      <div className="flex-1 p-6">
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

            <div className="bg-slate-900 border border-slate-800 rounded p-4 h-[400px] overflow-y-auto text-sm space-y-4">
              {Object.values(active.nodes).map((node) => (
                <div key={node.node_id} className="space-y-4">
                  {node.turns.map((turn, idx) => (
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

                          {/* Collapsible Reasoning */}
                          {turn.reasoning.length > 0 && (
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
                                🧠 Reasoning
                              </button>

                              {expandedReasoning[turn.injection_id] && (
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
              ))}
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
                      const apiBase = `${window.location.protocol}//${window.location.hostname}:4000`
                      await fetch(`${apiBase}/inject/${active.alias}`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ prompt: value })
                      })
                      ;(e.target as HTMLTextAreaElement).value = ''
                    } finally {
                      // brief delay to avoid rapid double send
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
