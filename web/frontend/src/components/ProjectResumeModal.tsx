'use client'

import { useEffect, useMemo, useState } from 'react'
import { getBackendHttpOrigin } from '@/lib/runtime'
import { useSwarmStore, type ProjectRecord } from '@/lib/store'

interface Props {
  project: ProjectRecord
  onClose: () => void
}

interface ResumePreview {
  blocked?: boolean
  blocked_reason?: string | null
  worker_swarm_ids?: string[]
  blocking_assignments?: Array<{
    task_id: string
    title: string
    swarm_id: string
    swarm_alias: string
    swarm_status?: string
    node_id?: number
    branch?: string
  }>
  blocking_swarms?: Array<{
    swarm_id: string
    swarm_alias: string
    swarm_status?: string
  }>
  counts_before?: Record<string, number>
  counts_after?: Record<string, number>
  summary?: Record<string, number>
  task_changes?: Array<{
    task_id: string
    title: string
    before_status: string
    after_status: string
    resume_decision?: string
    reason?: string
  }>
}

export default function ProjectResumeModal({ project, onClose }: Props) {
  const swarms = useSwarmStore((s) => s.swarms)
  const selectProject = useSwarmStore((s) => s.selectProject)
  const selectSwarm = useSwarmStore((s) => s.selectSwarm)
  const [workerSwarmIds, setWorkerSwarmIds] = useState<string[]>(project.worker_swarm_ids ?? [])
  const [retryFailed, setRetryFailed] = useState(false)
  const [reverifyCompleted, setReverifyCompleted] = useState(true)
  const [preview, setPreview] = useState<ResumePreview | null>(null)
  const [previewError, setPreviewError] = useState<string | null>(null)
  const [loadingPreview, setLoadingPreview] = useState(false)
  const [previewNonce, setPreviewNonce] = useState(0)
  const [terminatingSwarmIds, setTerminatingSwarmIds] = useState<string[]>([])
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

  useEffect(() => {
    const validIds = new Set(swarmOptions.map((swarm) => swarm.swarm_id))
    setWorkerSwarmIds((prev) => prev.filter((id) => validIds.has(id)))
  }, [swarmOptions])

  useEffect(() => {
    let cancelled = false
    const apiBase = getBackendHttpOrigin()
    setLoadingPreview(true)
    setPreviewError(null)
    fetch(`${apiBase}/projects/${project.project_id}/resume-preview`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        worker_swarm_ids: workerSwarmIds,
        retry_failed: retryFailed,
        reverify_completed: reverifyCompleted
      })
    })
      .then(async (res) => {
        if (!res.ok) {
          let message = `HTTP ${res.status}`
          try {
            const payload = await res.json()
            if (typeof payload?.error === 'string') message = payload.error
          } catch {}
          throw new Error(message)
        }
        return res.json()
      })
      .then((payload) => {
        if (cancelled) return
        setPreview(payload ?? null)
      })
      .catch((err) => {
        if (cancelled) return
        setPreview(null)
        setPreviewError(err instanceof Error ? err.message : 'Failed to load preview.')
      })
      .finally(() => {
        if (cancelled) return
        setLoadingPreview(false)
      })
    return () => {
      cancelled = true
    }
  }, [project.project_id, retryFailed, reverifyCompleted, previewNonce, JSON.stringify(workerSwarmIds.slice().sort())])

  async function terminateBlockingSwarm(_swarmAlias: string, swarmId: string) {
    try {
      setTerminatingSwarmIds((prev) => (prev.includes(swarmId) ? prev : [...prev, swarmId]))
      const apiBase = getBackendHttpOrigin()
      const res = await fetch(`${apiBase}/swarms/${encodeURIComponent(swarmId)}/terminate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ force: true })
      })
      if (!res.ok) {
        let message = `HTTP ${res.status}`
        try {
          const payload = await res.json()
          if (typeof payload?.error === 'string') message = payload.error
        } catch {}
        throw new Error(message)
      }
      setTimeout(() => setPreviewNonce((value) => value + 1), 700)
    } catch (err) {
      alert(err instanceof Error ? err.message : 'Failed to terminate blocking swarm.')
    } finally {
      setTerminatingSwarmIds((prev) => prev.filter((id) => id !== swarmId))
    }
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

          <div data-testid="project-resume-preview" className="rounded border border-slate-700 p-3 bg-slate-950/30 space-y-3">
            <div className="text-xs uppercase tracking-wide text-slate-500">Resume Preview</div>
            {loadingPreview ? (
              <div className="text-sm text-slate-500">Loading preview...</div>
            ) : previewError ? (
              <div className="text-sm text-rose-300">{previewError}</div>
            ) : preview ? (
              <>
                {preview.blocked && (
                  <div data-testid="project-resume-preview-blocked" className="rounded border border-rose-500/30 bg-rose-500/10 p-2 text-sm text-rose-300">
                    {preview.blocked_reason || 'Resume is currently blocked.'}
                  </div>
                )}
                {Array.isArray(preview.blocking_swarms) && preview.blocking_swarms.length > 0 && (
                  <div className="space-y-2">
                    <div className="text-xs uppercase tracking-wide text-slate-500">Blocking Swarms</div>
                    {preview.blocking_swarms.map((item) => (
                      <div
                        key={item.swarm_id}
                        data-testid={`project-resume-blocking-swarm-${item.swarm_id}`}
                        className="rounded border border-slate-800 bg-slate-900/60 p-2 flex items-center justify-between gap-3"
                      >
                        <div>
                          <div className="text-sm text-slate-200">{item.swarm_alias}</div>
                          <div className="text-xs text-slate-500">{item.swarm_status || 'running'}</div>
                        </div>
                        <div className="flex items-center gap-2">
                          <button
                            data-testid={`project-resume-focus-swarm-${item.swarm_id}`}
                            onClick={() => selectSwarm(item.swarm_id)}
                            className="px-2 py-1 rounded bg-slate-800 border border-slate-700 text-xs hover:bg-slate-700"
                          >
                            Focus
                          </button>
                          <button
                            data-testid={`project-resume-terminate-swarm-${item.swarm_id}`}
                            disabled={terminatingSwarmIds.includes(item.swarm_id)}
                            onClick={() => terminateBlockingSwarm(item.swarm_alias, item.swarm_id)}
                            className="px-2 py-1 rounded bg-rose-700/80 text-white text-xs disabled:opacity-50"
                          >
                            {terminatingSwarmIds.includes(item.swarm_id) ? 'Terminating...' : 'Force Terminate'}
                          </button>
                        </div>
                      </div>
                    ))}
                  </div>
                )}
                {Array.isArray(preview.blocking_assignments) && preview.blocking_assignments.length > 0 && (
                  <div className="space-y-2">
                    <div className="text-xs uppercase tracking-wide text-slate-500">Blocking Assignments</div>
                    {preview.blocking_assignments.map((item) => (
                      <div key={`${item.swarm_id}:${item.task_id}:${item.node_id ?? 'na'}`} className="rounded border border-slate-800 bg-slate-900/60 p-2">
                        <div className="text-sm text-slate-200">{item.title}</div>
                        <div className="text-xs text-slate-500">{item.task_id}</div>
                        <div className="text-xs text-slate-300 mt-1">
                          {item.swarm_alias}
                          {typeof item.node_id === 'number' ? ` · agent ${item.node_id}` : ''}
                        </div>
                        {item.branch && (
                          <div className="text-xs text-slate-500 mt-1">{item.branch}</div>
                        )}
                      </div>
                    ))}
                  </div>
                )}
                <div className="grid gap-3 md:grid-cols-2 text-xs text-slate-300">
                  <div>
                    <div className="text-slate-500 mb-1">Before</div>
                    <div>completed {preview.counts_before?.completed ?? 0}</div>
                    <div>assigned {preview.counts_before?.assigned ?? 0}</div>
                    <div>ready {preview.counts_before?.ready ?? 0}</div>
                    <div>failed {preview.counts_before?.failed ?? 0}</div>
                  </div>
                  <div>
                    <div className="text-slate-500 mb-1">After</div>
                    <div>completed {preview.counts_after?.completed ?? 0}</div>
                    <div>assigned {preview.counts_after?.assigned ?? 0}</div>
                    <div>ready {preview.counts_after?.ready ?? 0}</div>
                    <div>failed {preview.counts_after?.failed ?? 0}</div>
                  </div>
                </div>
                <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-300">
                  <span>kept {preview.summary?.kept_completed ?? 0}</span>
                  <span>recovered {preview.summary?.recovered_from_branch ?? 0}</span>
                  <span>reset {preview.summary?.reset_assigned ?? 0}</span>
                  <span>downgraded {preview.summary?.downgraded_to_pending ?? 0}</span>
                  <span>retried {preview.summary?.retried_failed ?? 0}</span>
                </div>
                {Array.isArray(preview.task_changes) && preview.task_changes.length > 0 ? (
                  <div className="space-y-2 max-h-44 overflow-y-auto pr-1">
                    {preview.task_changes.map((change) => (
                      <div key={change.task_id} className="rounded border border-slate-800 bg-slate-900/60 p-2">
                        <div className="text-sm text-slate-200">{change.title}</div>
                        <div className="text-xs text-slate-500">{change.task_id}</div>
                        <div className="text-xs text-slate-300 mt-1">
                          {change.before_status} {'->'} {change.after_status}
                        </div>
                        {change.resume_decision && (
                          <div className="text-xs text-sky-300 mt-1">{change.resume_decision}</div>
                        )}
                        {change.reason && (
                          <div className="text-xs text-amber-300 mt-1">{change.reason}</div>
                        )}
                      </div>
                    ))}
                  </div>
                ) : (
                  <div className="text-sm text-slate-500">No task state changes are predicted for this resume configuration.</div>
                )}
                <div>
                  <button
                    data-testid="project-resume-refresh-preview-button"
                    onClick={() => setPreviewNonce((value) => value + 1)}
                    className="px-2 py-1 rounded bg-slate-800 border border-slate-700 text-xs hover:bg-slate-700"
                  >
                    Refresh Preview
                  </button>
                </div>
              </>
            ) : (
              <div className="text-sm text-slate-500">No preview available.</div>
            )}
          </div>
        </div>

        <div className="flex justify-end mt-6 space-x-3 shrink-0">
          <button data-testid="project-resume-cancel-button" onClick={onClose} className="px-4 py-2 bg-slate-700 rounded">
            Cancel
          </button>
          <button
            data-testid="project-resume-submit-button"
            onClick={handleSubmit}
            disabled={submitting || swarmOptions.length === 0 || Boolean(preview?.blocked)}
            className="px-4 py-2 bg-cyan-600 rounded disabled:opacity-50"
          >
            {submitting ? 'Resuming...' : 'Resume Project'}
          </button>
        </div>
      </div>
    </div>
  )
}
