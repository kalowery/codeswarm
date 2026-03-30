'use client'

import { useMemo, useState } from 'react'
import { useSwarmStore } from '@/lib/store'
import { getBackendHttpOrigin } from '@/lib/runtime'

interface Props {
  onClose: () => void
}

type Mode = 'plan' | 'tasks'
type RepoMode = 'local' | 'github'

const DEFAULT_TASKS_JSON = JSON.stringify(
  [
    {
      task_id: 'T-001',
      title: 'Example task',
      prompt: 'Describe the work the implementation agent should perform.',
      acceptance_criteria: ['The change is implemented.', 'Verification was run and reported.'],
      depends_on: [],
      owned_paths: ['src/example.ts']
    }
  ],
  null,
  2
)

export default function ProjectModal({ onClose }: Props) {
  const swarms = useSwarmStore((s) => s.swarms)
  const selectSwarm = useSwarmStore((s) => s.selectSwarm)
  const [mode, setMode] = useState<Mode>('plan')
  const [title, setTitle] = useState('')
  const [repoMode, setRepoMode] = useState<RepoMode>('local')
  const [repoPath, setRepoPath] = useState('')
  const [githubOwner, setGithubOwner] = useState('')
  const [githubRepo, setGithubRepo] = useState('')
  const [githubCreateIfMissing, setGithubCreateIfMissing] = useState(true)
  const [githubVisibility, setGithubVisibility] = useState<'private' | 'public' | 'internal'>('private')
  const [baseBranch, setBaseBranch] = useState('main')
  const [workspaceSubdir, setWorkspaceSubdir] = useState('repo')
  const [plannerSwarmId, setPlannerSwarmId] = useState('')
  const [workerSwarmIds, setWorkerSwarmIds] = useState<string[]>([])
  const [spec, setSpec] = useState('')
  const [tasksJson, setTasksJson] = useState(DEFAULT_TASKS_JSON)
  const [autoStart, setAutoStart] = useState(true)
  const [submitting, setSubmitting] = useState(false)

  const swarmOptions = useMemo(
    () =>
      Object.values(swarms)
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
    if (!title.trim()) {
      alert('Project title is required.')
      return
    }
    if (repoMode === 'local' && !repoPath.trim()) {
      alert('Repository path is required.')
      return
    }
    if (repoMode === 'github' && (!githubOwner.trim() || !githubRepo.trim())) {
      alert('GitHub org and repo name are required.')
      return
    }
    if (workerSwarmIds.length === 0) {
      alert('Select at least one worker swarm.')
      return
    }
    if (mode === 'plan' && !plannerSwarmId) {
      alert('Select a planner swarm.')
      return
    }
    if (mode === 'plan' && !spec.trim()) {
      alert('Specification text is required in planner mode.')
      return
    }

    let parsedTasks: unknown = undefined
    if (mode === 'tasks') {
      try {
        parsedTasks = JSON.parse(tasksJson)
      } catch {
        alert('Tasks JSON is invalid.')
        return
      }
      if (!Array.isArray(parsedTasks)) {
        alert('Tasks JSON must be an array of task objects.')
        return
      }
    }

    try {
      setSubmitting(true)
      const apiBase = getBackendHttpOrigin()
      if (mode === 'plan') {
        const res = await fetch(`${apiBase}/projects/plan`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            title: title.trim(),
            repo_mode: repoMode === 'github' ? 'github' : 'local_path',
            repo_path: repoMode === 'local' ? repoPath.trim() : '',
            github_owner: repoMode === 'github' ? githubOwner.trim() : '',
            github_repo: repoMode === 'github' ? githubRepo.trim() : '',
            github_create_if_missing: repoMode === 'github' ? githubCreateIfMissing : false,
            github_visibility: repoMode === 'github' ? githubVisibility : undefined,
            spec,
            planner_swarm_id: plannerSwarmId,
            worker_swarm_ids: workerSwarmIds,
            base_branch: baseBranch.trim() || 'main',
            workspace_subdir: workspaceSubdir.trim() || 'repo',
            auto_start: autoStart
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
        if (plannerSwarmId) {
          selectSwarm(plannerSwarmId)
        }
      } else {
        const res = await fetch(`${apiBase}/projects`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            title: title.trim(),
            repo_mode: repoMode === 'github' ? 'github' : 'local_path',
            repo_path: repoMode === 'local' ? repoPath.trim() : '',
            github_owner: repoMode === 'github' ? githubOwner.trim() : '',
            github_repo: repoMode === 'github' ? githubRepo.trim() : '',
            github_create_if_missing: repoMode === 'github' ? githubCreateIfMissing : false,
            github_visibility: repoMode === 'github' ? githubVisibility : undefined,
            worker_swarm_ids: workerSwarmIds,
            tasks: parsedTasks,
            base_branch: baseBranch.trim() || 'main',
            workspace_subdir: workspaceSubdir.trim() || 'repo',
            auto_start: autoStart
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
        if (workerSwarmIds[0]) {
          selectSwarm(workerSwarmIds[0])
        }
      }
      onClose()
    } catch (err) {
      alert(err instanceof Error ? err.message : 'Project submission failed.')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div data-testid="project-modal" className="fixed inset-0 z-50 bg-black/75 flex items-center justify-center">
      <div className="bg-slate-900 border border-slate-800 rounded-lg w-[720px] h-[760px] max-h-[86vh] p-6 flex flex-col shadow-2xl">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold">Create Project</h2>
          <div className="inline-flex rounded border border-slate-700 overflow-hidden text-xs">
            <button
              data-testid="project-mode-plan"
              onClick={() => setMode('plan')}
              className={`px-3 py-1 ${mode === 'plan' ? 'bg-cyan-600 text-white' : 'bg-slate-900 text-slate-300 hover:bg-slate-800'}`}
            >
              Plan From Spec
            </button>
            <button
              data-testid="project-mode-tasks"
              onClick={() => setMode('tasks')}
              className={`px-3 py-1 ${mode === 'tasks' ? 'bg-cyan-600 text-white' : 'bg-slate-900 text-slate-300 hover:bg-slate-800'}`}
            >
              Direct Tasks
            </button>
          </div>
        </div>

        <div className="space-y-4 overflow-y-auto pr-1 flex-1 min-h-0">
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-sm text-slate-400 mb-1">Project Title</label>
              <input
                data-testid="project-title-input"
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                className="w-full bg-slate-800 border border-slate-700 rounded px-3 py-2"
                placeholder="Checkout flow rewrite"
              />
            </div>
            <div>
              <label className="block text-sm text-slate-400 mb-1">Base Branch</label>
              <input
                data-testid="project-base-branch-input"
                value={baseBranch}
                onChange={(e) => setBaseBranch(e.target.value)}
                className="w-full bg-slate-800 border border-slate-700 rounded px-3 py-2"
                placeholder="main"
              />
            </div>
          </div>

          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-sm text-slate-400 mb-1">Repository Source</label>
              <div className="inline-flex rounded border border-slate-700 overflow-hidden text-xs w-full">
                <button
                  data-testid="project-repo-mode-local"
                  onClick={() => setRepoMode('local')}
                  className={`flex-1 px-3 py-2 ${repoMode === 'local' ? 'bg-cyan-600 text-white' : 'bg-slate-900 text-slate-300 hover:bg-slate-800'}`}
                >
                  Local Path
                </button>
                <button
                  data-testid="project-repo-mode-github"
                  onClick={() => setRepoMode('github')}
                  className={`flex-1 px-3 py-2 ${repoMode === 'github' ? 'bg-cyan-600 text-white' : 'bg-slate-900 text-slate-300 hover:bg-slate-800'}`}
                >
                  GitHub Repo
                </button>
              </div>
            </div>
            <div>
              <label className="block text-sm text-slate-400 mb-1">Workspace Subdir</label>
              <input
                data-testid="project-workspace-subdir-input"
                value={workspaceSubdir}
                onChange={(e) => setWorkspaceSubdir(e.target.value)}
                className="w-full bg-slate-800 border border-slate-700 rounded px-3 py-2"
                placeholder="repo"
              />
            </div>
          </div>

          {repoMode === 'local' ? (
            <div>
              <label className="block text-sm text-slate-400 mb-1">Repository Path</label>
              <input
                data-testid="project-repo-path-input"
                value={repoPath}
                onChange={(e) => setRepoPath(e.target.value)}
                className="w-full bg-slate-800 border border-slate-700 rounded px-3 py-2"
                placeholder="/absolute/path/to/repo"
              />
            </div>
          ) : (
            <div className="rounded border border-slate-700 p-3 bg-slate-950/30 space-y-4">
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="block text-sm text-slate-400 mb-1">GitHub Org / Owner</label>
                  <input
                    data-testid="project-github-owner-input"
                    value={githubOwner}
                    onChange={(e) => setGithubOwner(e.target.value)}
                    className="w-full bg-slate-800 border border-slate-700 rounded px-3 py-2"
                    placeholder="AMDResearch"
                  />
                </div>
                <div>
                  <label className="block text-sm text-slate-400 mb-1">Repository Name</label>
                  <input
                    data-testid="project-github-repo-input"
                    value={githubRepo}
                    onChange={(e) => setGithubRepo(e.target.value)}
                    className="w-full bg-slate-800 border border-slate-700 rounded px-3 py-2"
                    placeholder="new-project"
                  />
                </div>
              </div>
              <div className="grid grid-cols-2 gap-4">
                <label className="inline-flex items-center gap-2 text-sm text-slate-300">
                  <input
                    data-testid="project-github-create-if-missing"
                    type="checkbox"
                    checked={githubCreateIfMissing}
                    onChange={(e) => setGithubCreateIfMissing(e.target.checked)}
                  />
                  <span>Create repo if missing</span>
                </label>
                <div>
                  <label className="block text-sm text-slate-400 mb-1">Visibility</label>
                  <select
                    data-testid="project-github-visibility-select"
                    value={githubVisibility}
                    onChange={(e) => setGithubVisibility(e.target.value as 'private' | 'public' | 'internal')}
                    className="w-full bg-slate-800 border border-slate-700 rounded px-3 py-2"
                  >
                    <option value="private">Private</option>
                    <option value="public">Public</option>
                    <option value="internal">Internal</option>
                  </select>
                </div>
              </div>
              <div className="text-xs text-slate-500">
                Codeswarm will create or reuse the GitHub repository, materialize a local control clone for planning and Beads sync, and clone from that source into each worker workspace.
              </div>
            </div>
          )}

          {mode === 'plan' && (
            <div>
              <label className="block text-sm text-slate-400 mb-1">Planner Swarm</label>
              <select
                data-testid="project-planner-swarm-select"
                value={plannerSwarmId}
                onChange={(e) => setPlannerSwarmId(e.target.value)}
                className="w-full bg-slate-800 border border-slate-700 rounded px-3 py-2"
              >
                <option value="">Select planner swarm…</option>
                {swarmOptions.map((swarm) => (
                  <option key={swarm.swarm_id} value={swarm.swarm_id}>
                    {swarm.alias} · {swarm.node_count} agent{swarm.node_count === 1 ? '' : 's'}
                  </option>
                ))}
              </select>
            </div>
          )}

          <div className="rounded border border-slate-700 p-3 bg-slate-950/30">
            <div className="text-xs uppercase tracking-wide text-slate-500 mb-2">Worker Swarms</div>
            {swarmOptions.length === 0 ? (
              <div className="text-sm text-slate-500">Launch one or more swarms before creating a project.</div>
            ) : (
              <div className="space-y-2">
                {swarmOptions.map((swarm) => (
                  <label key={swarm.swarm_id} className="flex items-center gap-2 text-sm text-slate-300">
                    <input
                      data-testid={`project-worker-swarm-${swarm.swarm_id}`}
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
              data-testid="project-auto-start-checkbox"
              type="checkbox"
              checked={autoStart}
              onChange={(e) => setAutoStart(e.target.checked)}
            />
            Start execution immediately after project creation
          </label>

          {mode === 'plan' ? (
            <div>
              <label className="block text-sm text-slate-400 mb-1">Specification</label>
              <textarea
                data-testid="project-spec-input"
                value={spec}
                onChange={(e) => setSpec(e.target.value)}
                className="w-full bg-slate-800 border border-slate-700 rounded px-3 py-2 h-72"
                placeholder="Describe the software you want built. The planner swarm will return a structured task graph."
              />
              <p className="mt-1 text-xs text-slate-500">
                After submit, the UI will focus the planner swarm so you can watch the decomposition work live.
              </p>
            </div>
          ) : (
            <div>
              <label className="block text-sm text-slate-400 mb-1">Tasks JSON</label>
              <textarea
                data-testid="project-tasks-json-input"
                value={tasksJson}
                onChange={(e) => setTasksJson(e.target.value)}
                className="w-full bg-slate-800 border border-slate-700 rounded px-3 py-2 h-72 font-mono text-sm"
              />
              <p className="mt-1 text-xs text-slate-500">
                Provide an array of task objects with `task_id`, `title`, `prompt`, `acceptance_criteria`, and `depends_on`.
              </p>
            </div>
          )}
        </div>

        <div className="flex justify-end mt-6 space-x-3 shrink-0">
          <button data-testid="project-cancel-button" onClick={onClose} className="px-4 py-2 bg-slate-700 rounded">
            Cancel
          </button>
          <button
            data-testid="project-submit-button"
            onClick={handleSubmit}
            disabled={submitting || swarmOptions.length === 0}
            className="px-4 py-2 bg-cyan-600 rounded disabled:opacity-50"
          >
            {submitting ? 'Submitting...' : mode === 'plan' ? 'Plan Project' : 'Create Project'}
          </button>
        </div>
      </div>
    </div>
  )
}
