'use client'

import { useState } from 'react'

interface Props {
  onClose: () => void
}

export default function LaunchModal({ onClose }: Props) {
  const [alias, setAlias] = useState('')
  const [nodes, setNodes] = useState('1')
  const [prompt, setPrompt] = useState('')
  const [loading, setLoading] = useState(false)

  async function handleLaunch() {
    const nodeCount = parseInt(nodes, 10)

    if (isNaN(nodeCount) || nodeCount < 1) {
      alert('Node count must be at least 1')
      return
    }

    try {
      setLoading(true)
      const apiBase = `${window.location.protocol}//${window.location.hostname}:4000`
      const res = await fetch(`${apiBase}/launch`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ nodes: nodeCount, prompt, alias })
      })

      const data = await res.json()

      if (data.request_id) {
        const { addPendingLaunch } = require('@/lib/store').useSwarmStore.getState()
        addPendingLaunch(data.request_id, alias || 'Launching swarm')
      }

      onClose()
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center">
      <div className="bg-slate-900 border border-slate-800 rounded-lg w-[500px] p-6">
        <h2 className="text-lg font-semibold mb-4">Launch Swarm</h2>

        <div className="space-y-4">
          <div>
            <label className="block text-sm text-slate-400 mb-1">Alias</label>
            <input
              value={alias}
              onChange={(e) => setAlias(e.target.value)}
              className="w-full bg-slate-800 border border-slate-700 rounded px-3 py-2"
              placeholder="optional-name"
            />
          </div>

          <div>
            <label className="block text-sm text-slate-400 mb-1">Nodes</label>
            <input
              type="number"
              min={1}
              value={nodes}
              onChange={(e) => setNodes(e.target.value)}
              className="w-full bg-slate-800 border border-slate-700 rounded px-3 py-2"
            />
          </div>

          <div>
            <label className="block text-sm text-slate-400 mb-1">System Prompt</label>
            <textarea
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              className="w-full bg-slate-800 border border-slate-700 rounded px-3 py-2 h-24"
              placeholder="You are a skilled GPU programmer..."
            />
          </div>
        </div>

        <div className="flex justify-end mt-6 space-x-3">
          <button
            onClick={onClose}
            className="px-4 py-2 bg-slate-700 rounded"
          >
            Cancel
          </button>
          <button
            onClick={handleLaunch}
            disabled={loading}
            className="px-4 py-2 bg-indigo-600 rounded disabled:opacity-50"
          >
            {loading ? 'Launching...' : 'Launch'}
          </button>
        </div>
      </div>
    </div>
  )
}
