'use client'

import { useMemo, useState } from 'react'
import { getBackendHttpOrigin } from '@/lib/runtime'
import { useSwarmStore, type ProjectRecord } from '@/lib/store'

interface Props {
  project: ProjectRecord
  onClose: () => void
}

export default function ProjectResumeModal({ project, onClose }: Props) {
  const swarms = useSwarmStore((s) => s.swarms)
  const selectProject = useSwarmStore((s) => s.selectProject)
  const [workerSwarmIds, setWorkerSwarmIds] = useState<string[]>(project.worker_swarm_ids ?? [])
  const [retryFailed, setRetryFailed] = useState(false)
  const [reverifyCompleted, setReverifyCompleted] = useState(true)
  const [submitting, setSubmitting] = useState(false)

  const swarmOptions = useMemo(
    () =>
      Object.values(swarms)
        .filter((swarm) => (swarm.status || '').toLowerCase() !== 'terminated')
        .slice()
        .sort((a, b) => a.alias.localeCompare(b.alias)),
    [swarms]
  )

  const toggleWorker = (swarmId: string) => {
    setWorkerSwarmIds((prev) =>
      prev.includes(swarmId) ? prev.filter((id) => id !== swarmId) : [...prev, swarmId]
    )
  }

  async function handleSubmit() {
    if (workerSwarmIds.length === 0) {
      alert('Select at least one worker swarm.')
      return
    }
    try {
      setSubmitting(true)
      const apiBase = getBackendHttpOrigin()
      const res = await fetch(`${apiBase}/projects/${project.project_id}/resume`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          worker_swarm_ids: workerSwarmIds,
          retry_failed: retryFailed,
          reverify_completed: reverifyCompleted
        })
      })
      if (!res.ok) {
        let message = `HTTP ${res.status}`
        try {
          const payload = await res.json()
          if (typeof payload?.error === 'string') message = payload.error
        } catch {}
        throw new Error(message)
      }
      selectProject(project.project_id)
      onClose()
    } catch (err) {
      alert(err instanceof Error ? err.message : 'Project resume failed.')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div data-testid="project-resume-modal" className="fixed inset-0 z-50 bg-black/75 flex items-center justify-center">
      <div className="bg-slate-900 border border-slate-800 rounded-lg w-[560px] max-h-[82vh] p-6 flex flex-col shadow-2xl">
        <div className="flex items-start justify-between gap-4 mb-4">
          <div>
            <h2 className="text-lg font-semibold">Resume Project</h2>
            <div className="text-sm text-slate-400 mt-1">{project.title}</div>
          </div>
        </div>

        <div className="space-y-4 overflow-y-auto pr-1 flex-1 min-h-0">
          <div className="rounded border border-slate-800 bg-slate-950/50 p-3 text-sm text-slate-300">
            Resume reconciles project state against durable task branches in the canonical repo, then dispatches remaining work to the selected worker swarms.
          </div>

          <div className="rounded border border-slate-700 p-3 bg-slate-950/30">
            <div className="text-xs uppercase tracking-wide text-slate-500 mb-2">Worker Swarms</div>
            {swarmOptions.length === 0 ? (
              <div className="text-sm text-slate-500">Launch one or more swarms before resuming the project.</div>
            ) : (
              <div className="space-y-2">
                {swarmOptions.map((swarm) => (
                  <label key={swarm.swarm_id} className="flex items-center gap-2 text-sm text-slate-300">
                    <input
                      data-testid={`project-resume-worker-${swarm.swarm_id}`}
                      type="checkbox"
                      checked={workerSwarmIds.includes(swarm.swarm_id)}
                      onChange={() => toggleWorker(swarm.swarm_id)}
                    />
                    <span>{swarm.alias}</span>
                    <span className="text-xs text-slate-500">
                      {swarm.node_count} agent{swarm.node_count === 1 ? '' : 's'} · {swarm.status}
                    </span>
                  </label>
                ))}
              </div>
            )}
          </div>

          <label className="inline-flex items-center gap-2 text-sm text-slate-300">
            <input
              data-testid="project-resume-reverify-checkbox"
              type="checkbox"
              checked={reverifyCompleted}
              onChange={(e) => setReverifyCompleted(e.target.checked)}
            />
            <span>Reverify completed tasks from durable task branches</span>
          </label>

          <label className="inline-flex items-center gap-2 text-sm text-slate-300">
            <input
              data-testid="project-resume-retry-failed-checkbox"
              type="checkbox"
              checked={retryFailed}
              onChange={(e) => setRetryFailed(e.target.checked)}
            />
            <span>Retry failed tasks</span>
          </label>
        </div>

        <div className="flex justify-end mt-6 space-x-3 shrink-0">
          <button data-testid="project-resume-cancel-button" onClick={onClose} className="px-4 py-2 bg-slate-700 rounded">
            Cancel
          </button>
          <button
            data-testid="project-resume-submit-button"
            onClick={handleSubmit}
            disabled={submitting || swarmOptions.length === 0}
            className="px-4 py-2 bg-cyan-600 rounded disabled:opacity-50"
          >
            {submitting ? 'Resuming...' : 'Resume Project'}
          </button>
        </div>
      </div>
    </div>
  )
}
