'use client'

import { useEffect, useState, useRef } from 'react'
import { useSwarmStore } from '@/lib/store'
import { useWebSocket } from '@/lib/useWebSocket'
import LaunchModal from '@/components/LaunchModal'
import Image from 'next/image'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

function normalizeMarkdown(content: string, phase: string) {
  if (phase !== 'completed') return content

  const fenceMatches = content.match(/```/g)
  if (!fenceMatches || fenceMatches.length !== 2) return content

  const fencePattern = /^([\s\S]*?)^```markdown[ \t]*\r?\n([\s\S]*?)^```[ \t]*\s*$/m
  const match = content.match(fencePattern)
  if (!match) return content

  return match[2].trim()
}

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
    if (last.phase !== 'completed') return false
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
    setTimeout(updateScrollButtons, 0)
    return () => window.removeEventListener('resize', handleResize)
  }, [active])

  const [isSending, setIsSending] = useState(false)
  const [isTerminating, setIsTerminating] = useState(false)
  const [dotCount, setDotCount] = useState(0)

  useEffect(() => {
    const id = setInterval(() => setDotCount((d) => (d + 1) % 4), 500)
    return () => clearInterval(id)
  }, [])

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100 flex overflow-x-hidden">
      <div className="w-80 shrink-0 border-r border-slate-800 p-4">
        <div className="mb-4 space-y-3">
          <div className="flex items-center gap-3">
            <Image src="/codeswarm.jpg" alt="Codeswarm" width={32} height={32} className="rounded-lg ring-1 ring-slate-700" />
            <div>
              <div className="text-lg font-semibold tracking-wide">Codeswarm</div>
              <div className="text-xs text-slate-500 -mt-1">Swarm Control</div>
            </div>
          </div>

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

            <button onClick={() => setShowLaunch(true)} className="px-2 py-1 bg-indigo-600 rounded text-sm">+ Launch</button>
          </div>
        </div>

        <div className="space-y-2">
          {launchError && (
            <div className="p-3 rounded border bg-rose-900 border-rose-500 text-rose-200 text-sm">
              <div className="flex justify-between items-start gap-2">
                <div className="flex-1 min-w-0">
                  ⚠ Launch failed:
                  <div className="mt-1 text-xs whitespace-pre-wrap break-words max-h-40 overflow-y-auto">{launchError}</div>
                </div>
                <button onClick={clearLaunchError} className="text-xs text-rose-300 hover:underline">Dismiss</button>
              </div>
            </div>
          )}

          {Object.entries(pendingLaunches).map(([reqId, launch]) => (
            <div key={reqId} className="p-3 rounded border bg-slate-900 border-amber-500">
              <div className="font-medium flex items-center gap-2">
                {launch.alias}
                <span className="inline-block w-2 h-2 rounded-full bg-amber-400 animate-pulse" />
              </div>
              <div className="text-sm text-amber-400">LAUNCHING...</div>
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

      <div className="flex-1 min-w-0 p-6">
        {!active && <div className="text-slate-500">Select a swarm to view details</div>}

        {active && (
          <div>
            <div className="flex items-center justify-between mb-4">
              <div>
                <h1 className="text-xl font-semibold">{active.alias}</h1>
                <div className="text-sm text-slate-400">
                  Status: {active.status ?? 'unknown'} · Slurm: {active.slurm_state}
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
                    await fetch(`${apiBase}/terminate/${active.alias}`, { method: 'POST' })
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
              {(() => {
                const activeNodeBySwarm = useSwarmStore.getState().activeNodeBySwarm
                const setActiveNode = useSwarmStore.getState().setActiveNode
                const activeNodeId = activeNodeBySwarm[active.swarm_id] ?? 0
                const activeNode = active.nodes[activeNodeId]

                return (
                  <>
                    <div className="mb-3 border-b border-slate-800 pb-3">
                      <div className="text-xs text-slate-500 mb-2">
                        Nodes ({Object.keys(active.nodes).length})
                      </div>
                      <div ref={nodeScrollRef} onScroll={updateScrollButtons} className="flex flex-nowrap gap-2 overflow-x-auto w-full">
                        {Object.keys(active.nodes).map((nodeId) => {
                          const id = Number(nodeId)
                          const isActive = id === activeNodeId
                          const needsAttention = nodeNeedsAttention(active.swarm_id, id)
                          const node = active.nodes[id]
                          const lastTurn = node.turns[node.turns.length - 1]
                          const isWorking = lastTurn && lastTurn.phase !== 'completed'

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
                              Node {id}
                            </button>
                          )
                        })}
                      </div>
                    </div>

                    <div className="space-y-4">
                      {activeNode.turns.map((turn, idx) => (
                        <div key={idx} className="space-y-2">
                          {turn.prompt && (
                            <div className="flex justify-end">
                              <div className="max-w-[75%] bg-indigo-600 text-white px-3 py-2 rounded-lg rounded-br-sm">
                                {turn.prompt}
                              </div>
                            </div>
                          )}

                          <div className="flex justify-start">
                            <div className="max-w-[75%] bg-slate-800 border border-slate-700 px-3 py-2 rounded-lg rounded-bl-sm space-y-2">

                              {turn.phase !== 'completed' && turn.phase !== 'error' && (
                                <div className="flex items-center gap-2 text-[10px] text-emerald-400">
                                  <span className="inline-block w-2 h-2 rounded-full bg-emerald-500 animate-pulse" />
                                  <span>
                                    {turn.phase === 'awaiting_approval'
                                      ? 'Awaiting approval'
                                      : turn.phase === 'executing'
                                      ? 'Executing'
                                      : 'Working'}
                                    {'.'.repeat(dotCount)}
                                  </span>
                                </div>
                              )}

                              {turn.reasoning && (
                                <details className="text-xs text-amber-400">
                                  <summary className="cursor-pointer select-none text-amber-300">
                                    Reasoning
                                  </summary>
                                  <div className="mt-1 whitespace-pre-wrap">
                                    {turn.reasoning}
                                  </div>
                                </details>
                              )}

                              {turn.phase === 'awaiting_approval' && turn.approval && (
                                <div className="text-xs bg-amber-900 border border-amber-500 rounded p-2">
                                  <div className="text-amber-300 mb-1">Execution approval required</div>
                                  <div className="text-slate-200">
                                    $ {Array.isArray(turn.approval.command)
                                      ? turn.approval.command.join(' ')
                                      : turn.approval.command}
                                  </div>
                                  <div className="mt-1 text-slate-300">{turn.approval.reason}</div>

                                  <div className="mt-2 flex gap-2">
                                    <button
                                      className="px-2 py-1 bg-emerald-600 rounded text-xs hover:bg-emerald-500"
                                      onClick={async () => {
                                        const apiBase = `${window.location.protocol}//${window.location.hostname}:4000`
                                        await fetch(`${apiBase}/approval`, {
                                          method: 'POST',
                                          headers: { 'Content-Type': 'application/json' },
                                          body: JSON.stringify({
                                            job_id: active.job_id,
                                            call_id: turn.approval.call_id,
                                            approved: true
                                          })
                                        })
                                      }}
                                    >
                                      Approve
                                    </button>

                                    <button
                                      className="px-2 py-1 bg-rose-600 rounded text-xs hover:bg-rose-500"
                                      onClick={async () => {
                                        const apiBase = `${window.location.protocol}//${window.location.hostname}:4000`
                                        await fetch(`${apiBase}/approval`, {
                                          method: 'POST',
                                          headers: { 'Content-Type': 'application/json' },
                                          body: JSON.stringify({
                                            job_id: active.job_id,
                                            call_id: turn.approval.call_id,
                                            approved: false
                                          })
                                        })
                                      }}
                                    >
                                      Deny
                                    </button>
                                  </div>
                                </div>
                              )}

                              {turn.execution && (
                                <div className="text-xs bg-slate-900 border border-slate-700 rounded p-2">
                                  <div className="text-slate-400">
                                    $ {Array.isArray(turn.execution.command)
                                      ? turn.execution.command.join(' ')
                                      : turn.execution.command}
                                  </div>
                                  {turn.execution.stdout && (
                                    <div className="mt-1 text-emerald-400 whitespace-pre-wrap break-words overflow-x-auto">
                                      {turn.execution.stdout}
                                    </div>
                                  )}
                                  {turn.execution.status === 'completed' && (
                                    <div className="text-[10px] text-slate-500 mt-1">
                                      Exit {turn.execution.exit_code}
                                    </div>
                                  )}
                                </div>
                              )}

                              {turn.deltas.length > 0 && (() => {
                                const raw = turn.deltas.join('').trim()

                                if (turn.phase !== 'completed') {
                                  return (
                                    <div className="markdown-content break-words overflow-x-auto text-sm leading-relaxed">
                                      <ReactMarkdown remarkPlugins={[remarkGfm]}>
                                        {raw}
                                      </ReactMarkdown>
                                    </div>
                                  )
                                }

                                const formatted = normalizeMarkdown(raw, turn.phase)
                                const showRaw = raw !== formatted

                                return (
                                  <div className="markdown-content break-words overflow-x-auto text-sm leading-relaxed space-y-2">
                                    {showRaw && (
                                      <details className="text-xs text-slate-300">
                                        <summary className="cursor-pointer select-none text-slate-400">
                                          Raw Output
                                        </summary>
                                        <div className="mt-1 whitespace-pre-wrap">
                                          {raw}
                                        </div>
                                      </details>
                                    )}
                                    <ReactMarkdown remarkPlugins={[remarkGfm]}>
                                      {formatted}
                                    </ReactMarkdown>
                                  </div>
                                )
                              })()}

                              {turn.phase === 'error' && turn.error && (
                                <div className="text-rose-400 text-xs">
                                  ⚠ {turn.error}
                                </div>
                              )}

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
                      const store = useSwarmStore.getState()
                      const swarm = store.swarms[active.swarm_id]
                      const activeNodeId = store.activeNodeBySwarm[active.swarm_id] ?? 0

                      const trimmed = value.trim()
                      let promptText = trimmed
                      let targetNodes: number[] | 'all' = [activeNodeId]
                      const nodeIdSet = new Set(Object.keys(swarm.nodes).map((id) => Number(id)))

                      const allMatch = trimmed.match(/^\/all\s+([\s\S]+)$/)
                      if (allMatch) {
                        promptText = allMatch[1].trim()
                        if (!promptText) return
                        targetNodes = 'all'
                      } else {
                        const nodeMatch = trimmed.match(/^\/node\[(.+?)\]\s*([\s\S]+)$/)
                        if (nodeMatch) {
                          const expr = nodeMatch[1].trim()
                          promptText = nodeMatch[2].trim()
                          if (!promptText) return
                          const resolved = new Set<number>()
                          expr.split(',').forEach((part) => {
                            const chunk = part.trim()
                            if (!chunk) return
                            if (/^\d+$/.test(chunk)) {
                              resolved.add(Number(chunk))
                              return
                            }
                            const rangeMatch = chunk.match(/^(\d+)\s*-\s*(\d+)$/)
                            if (!rangeMatch) return
                            const start = Number(rangeMatch[1])
                            const end = Number(rangeMatch[2])
                            if (start > end) return
                            for (let i = start; i <= end; i += 1) {
                              resolved.add(i)
                            }
                          })
                          const resolvedNodes = Array.from(resolved).filter((id) => nodeIdSet.has(id))
                          if (resolvedNodes.length === 0) return
                          targetNodes = resolvedNodes
                        }
                      }

                      setPendingPrompt(promptText)
                      const updatedNodes = { ...swarm.nodes }
                      const nodeIds =
                        targetNodes === 'all'
                          ? Object.keys(swarm.nodes).map((id) => Number(id))
                          : targetNodes

                      nodeIds.forEach((nodeId) => {
                        const provisional = {
                          injection_id: `temp-${Date.now()}-${nodeId}`,
                          prompt: promptText,
                          deltas: [],
                          reasoning: '',
                          phase: 'streaming'
                        }

                        updatedNodes[nodeId] = {
                          ...updatedNodes[nodeId],
                          turns: [...updatedNodes[nodeId].turns, provisional]
                        }
                      })

                      store.addOrUpdateSwarm({
                        ...swarm,
                        nodes: updatedNodes
                      })

                      const apiBase = `${window.location.protocol}//${window.location.hostname}:4000`
                      const payload =
                        targetNodes === 'all'
                          ? { prompt: promptText }
                          : { prompt: promptText, nodes: nodeIds }
                      await fetch(`${apiBase}/inject/${active.alias}`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(payload)
                      })

                      ;(e.target as HTMLTextAreaElement).value = ''
                    } finally {
                      setTimeout(() => setIsSending(false), 300)
                    }
                  }
                }}
              />
              <div className="text-xs text-slate-500 mt-1">Press Enter to send (Shift+Enter for newline)</div>
            </div>
          </div>
        )}
      </div>

      {showLaunch && <LaunchModal onClose={() => setShowLaunch(false)} />}
    </div>
  )
}
