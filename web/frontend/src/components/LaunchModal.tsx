'use client'

import { useEffect, useMemo, useState } from 'react'

interface Props {
  onClose: () => void
}

interface ProviderFieldOption {
  label?: string
  value: string
}

interface ProviderLaunchField {
  key: string
  label?: string
  type?: 'text' | 'number' | 'boolean' | 'select'
  default?: string | number | boolean
  required?: boolean
  placeholder?: string
  options?: ProviderFieldOption[]
}

interface ProviderLaunchPanel {
  id?: string
  title?: string
  description?: string
  fields?: ProviderLaunchField[]
}

interface LaunchProvider {
  id: string
  label?: string
  backend: string
  defaults?: Record<string, any>
  launch_fields?: ProviderLaunchField[]
  launch_panels?: ProviderLaunchPanel[]
}

interface AgentsSkillFile {
  path: string
  content: string
}

interface AgentsBundlePayload {
  mode: 'file' | 'directory'
  agents_md_content: string
  skills_files: AgentsSkillFile[]
}

export default function LaunchModal({ onClose }: Props) {
  const [alias, setAlias] = useState('')
  const [nodes, setNodes] = useState('1')
  const [prompt, setPrompt] = useState('')
  const [agentsMdName, setAgentsMdName] = useState('')
  const [agentsMdContent, setAgentsMdContent] = useState('')
  const [agentsBundle, setAgentsBundle] = useState<AgentsBundlePayload | null>(null)
  const [providers, setProviders] = useState<LaunchProvider[]>([])
  const [selectedProvider, setSelectedProvider] = useState('')
  const [providerValues, setProviderValues] = useState<Record<string, string>>({})
  const [activeTab, setActiveTab] = useState<'general' | 'provider'>('general')
  const [loading, setLoading] = useState(false)
  const [loadingProviders, setLoadingProviders] = useState(true)
  const [providersError, setProvidersError] = useState<string | null>(null)

  useEffect(() => {
    const apiBase = `${window.location.protocol}//${window.location.hostname}:4000`
    setLoadingProviders(true)
    setProvidersError(null)
    let cancelled = false
    const delays = [0, 500, 1000, 2000]
    const load = async () => {
      for (let i = 0; i < delays.length; i += 1) {
        if (cancelled) return
        const delay = delays[i] ?? 0
        if (delay > 0) {
          await new Promise((resolve) => setTimeout(resolve, delay))
          if (cancelled) return
        }
        try {
          const res = await fetch(`${apiBase}/providers`)
          if (!res.ok) throw new Error(`HTTP ${res.status}`)
          const data = await res.json()
          const list = Array.isArray(data) ? (data as LaunchProvider[]) : []
          if (list.length === 0) {
            if (i < delays.length - 1) continue
            setProviders([])
            setProvidersError('No providers returned. Check router config and backend connection.')
            return
          }
          setProviders(list)
          setProvidersError(null)
          setSelectedProvider((current) =>
            current && list.some((p) => p.id === current) ? current : list[0].id
          )
          return
        } catch {
          if (i === delays.length - 1) {
            setProviders([])
            setProvidersError('Unable to load providers from backend.')
          }
        }
      }
    }
    load().finally(() => {
      if (!cancelled) setLoadingProviders(false)
    })
    return () => {
      cancelled = true
    }
  }, [])

  const activeProvider = useMemo(
    () => providers.find((p) => p.id === selectedProvider),
    [providers, selectedProvider]
  )

  useEffect(() => {
    if (!activeProvider) {
      setProviderValues({})
      return
    }
    const defaults: Record<string, string> = {}
    const panelFields = (activeProvider.launch_panels ?? [])
      .flatMap((panel) => (Array.isArray(panel.fields) ? panel.fields : []))
    const fields = [
      ...(Array.isArray(activeProvider.launch_fields) ? activeProvider.launch_fields : []),
      ...panelFields
    ]
    for (const field of fields) {
      const providerDefault = activeProvider.defaults?.[field.key]
      const fieldDefault = typeof field.default !== 'undefined' ? field.default : providerDefault
      if (typeof fieldDefault === 'undefined' || fieldDefault === null) continue
      defaults[field.key] = String(fieldDefault)
    }
    setProviderValues(defaults)
  }, [activeProvider])

  async function handleLaunch() {
    const nodeCount = parseInt(nodes, 10)

    if (isNaN(nodeCount) || nodeCount < 1) {
      alert('Agent count must be at least 1')
      return
    }

    try {
      setLoading(true)
      const apiBase = `${window.location.protocol}//${window.location.hostname}:4000`
      const provider_params: Record<string, string | number | boolean> = {}
      const allProviderFields = activeProvider
        ? [
            ...(Array.isArray(activeProvider.launch_fields) ? activeProvider.launch_fields : []),
            ...(activeProvider.launch_panels ?? []).flatMap((panel) => (Array.isArray(panel.fields) ? panel.fields : []))
          ]
        : []
      if (allProviderFields.length > 0) {
        for (const field of allProviderFields) {
          const raw = providerValues[field.key] ?? ''
          const required = !!field.required
          const type = field.type ?? 'text'
          if (required && raw.trim() === '') {
            alert(`Missing required field: ${field.label ?? field.key}`)
            setLoading(false)
            return
          }
          if (raw.trim() === '') continue
          if (type === 'number') {
            const n = Number(raw)
            if (Number.isNaN(n)) {
              alert(`Invalid number for ${field.label ?? field.key}`)
              setLoading(false)
              return
            }
            provider_params[field.key] = n
          } else if (type === 'boolean') {
            provider_params[field.key] = raw === 'true'
          } else {
            provider_params[field.key] = raw
          }
        }
      }

      const res = await fetch(`${apiBase}/launch`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          nodes: nodeCount,
          prompt,
          alias,
          agents_md_content: agentsMdContent || undefined,
          agents_bundle: agentsBundle || undefined,
          provider: selectedProvider || undefined,
          provider_params: Object.keys(provider_params).length > 0 ? provider_params : undefined
        })
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

  const renderField = (field: ProviderLaunchField) => {
    const type = field.type ?? 'text'
    const label = field.label ?? field.key
    const value = providerValues[field.key] ?? ''
    if (type === 'boolean') {
      return (
        <label key={field.key} className="flex items-center gap-2 text-sm text-slate-300">
          <input
            type="checkbox"
            checked={value === 'true'}
            onChange={(e) =>
              setProviderValues((prev) => ({ ...prev, [field.key]: e.target.checked ? 'true' : 'false' }))
            }
          />
          {label}
        </label>
      )
    }
    if (type === 'select' && Array.isArray(field.options) && field.options.length > 0) {
      return (
        <div key={field.key}>
          <label className="block text-sm text-slate-400 mb-1">{label}</label>
          <select
            value={value}
            onChange={(e) => setProviderValues((prev) => ({ ...prev, [field.key]: e.target.value }))}
            className="w-full bg-slate-800 border border-slate-700 rounded px-3 py-2"
          >
            <option value="">(default)</option>
            {field.options.map((opt) => (
              <option key={`${field.key}-${opt.value}`} value={opt.value}>
                {opt.label ?? opt.value}
              </option>
            ))}
          </select>
        </div>
      )
    }
    return (
      <div key={field.key}>
        <label className="block text-sm text-slate-400 mb-1">
          {label}{field.required ? ' *' : ''}
        </label>
        <input
          type={type === 'number' ? 'number' : 'text'}
          value={value}
          onChange={(e) => setProviderValues((prev) => ({ ...prev, [field.key]: e.target.value }))}
          className="w-full bg-slate-800 border border-slate-700 rounded px-3 py-2"
          placeholder={field.placeholder}
        />
      </div>
    )
  }

  const clearAgentsSelection = () => {
    setAgentsMdName('')
    setAgentsMdContent('')
    setAgentsBundle(null)
  }

  const handleAgentsSelection = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const selected = e.target.files
    if (!selected || selected.length === 0) {
      clearAgentsSelection()
      return
    }

    const files = Array.from(selected)
    const hasDirectoryPaths = files.some((f) => {
      const rel = String((f as any).webkitRelativePath || '')
      return rel.includes('/')
    })

    if (!hasDirectoryPaths) {
      const file = files[0]
      const text = await file.text()
      setAgentsMdName(file.name)
      setAgentsMdContent(text)
      setAgentsBundle({
        mode: 'file',
        agents_md_content: text,
        skills_files: []
      })
      return
    }

    const firstRel = String((files[0] as any).webkitRelativePath || '')
    const rootName = firstRel.split('/')[0]
    if (!rootName) {
      alert('Unable to resolve selected directory.')
      clearAgentsSelection()
      return
    }

    const agentsPath = `${rootName}/AGENTS.md`
    const agentsFile = files.find((f) => String((f as any).webkitRelativePath || '') === agentsPath)
    if (!agentsFile) {
      alert('Selected directory must contain AGENTS.md at its root.')
      clearAgentsSelection()
      return
    }

    const skillsPrefix = `${rootName}/skills/`
    const skillEntries = files
      .map((file) => {
        const rel = String((file as any).webkitRelativePath || '')
        if (!rel.startsWith(skillsPrefix)) return null
        const skillRelPath = rel.slice(skillsPrefix.length)
        if (!skillRelPath || skillRelPath.endsWith('/')) return null
        return { file, skillRelPath }
      })
      .filter((entry): entry is { file: File; skillRelPath: string } => entry !== null)

    const agentsText = await agentsFile.text()
    const skillsFiles: AgentsSkillFile[] = await Promise.all(
      skillEntries.map(async ({ file, skillRelPath }) => ({
        path: skillRelPath,
        content: await file.text()
      }))
    )

    setAgentsMdName(`${rootName}/ (persona directory)`)
    setAgentsMdContent(agentsText)
    setAgentsBundle({
      mode: 'directory',
      agents_md_content: agentsText,
      skills_files: skillsFiles
    })
  }

  const renderProviderFields = () => {
    const panels = activeProvider?.launch_panels ?? []
    if (panels.length > 0) {
      return (
        <div className="space-y-3">
          {panels.map((panel, panelIdx) => {
            const fields = Array.isArray(panel.fields) ? panel.fields : []
            if (fields.length === 0) return null
            return (
              <div
                key={panel.id ?? `${activeProvider?.id}-panel-${panelIdx}`}
                className="space-y-3 rounded border border-slate-700 p-3 bg-slate-950/30"
              >
                <div className="text-xs uppercase tracking-wide text-slate-500">
                  {panel.title ?? `Provider Panel ${panelIdx + 1}`}
                </div>
                {panel.description && (
                  <div className="text-xs text-slate-500">{panel.description}</div>
                )}
                {fields.map((field) => renderField(field))}
              </div>
            )
          })}
        </div>
      )
    }

    const fields = activeProvider?.launch_fields ?? []
    if (fields.length === 0) {
      return (
        <div className="text-sm text-slate-500">
          No provider-specific parameters for this provider.
        </div>
      )
    }
    return (
      <div className="space-y-3 rounded border border-slate-700 p-3 bg-slate-950/30">
        <div className="text-xs uppercase tracking-wide text-slate-500">Provider Parameters</div>
        {fields.map((field) => renderField(field))}
      </div>
    )
  }

  const hasProviderParams = (() => {
    if (!activeProvider) return false
    const panelFields = (activeProvider.launch_panels ?? []).some(
      (panel) => Array.isArray(panel.fields) && panel.fields.length > 0
    )
    if (panelFields) return true
    return Array.isArray(activeProvider.launch_fields) && activeProvider.launch_fields.length > 0
  })()

  const showProviderTab = hasProviderParams

  useEffect(() => {
    if (!showProviderTab && activeTab === 'provider') {
      setActiveTab('general')
    }
  }, [showProviderTab, activeTab])

  return (
    <div className="fixed inset-0 z-50 bg-black/75 flex items-center justify-center">
      <div className="bg-slate-900 border border-slate-800 rounded-lg w-[560px] h-[680px] max-h-[82vh] p-6 flex flex-col shadow-2xl">
        <h2 className="text-lg font-semibold mb-4">Launch Swarm</h2>

        <div className="mb-4">
          <label className="block text-sm text-slate-400 mb-1">Provider</label>
          {loadingProviders ? (
            <div className="text-sm text-slate-500">Loading providers...</div>
          ) : providersError ? (
            <div className="text-sm text-rose-400">{providersError}</div>
          ) : (
            <select
              value={selectedProvider}
              onChange={(e) => setSelectedProvider(e.target.value)}
              className="w-full bg-slate-800 border border-slate-700 rounded px-3 py-2"
            >
              {providers.map((provider) => (
                <option key={provider.id} value={provider.id}>
                  {provider.label || provider.id}
                </option>
              ))}
            </select>
          )}
        </div>

        {showProviderTab && (
          <div className="inline-flex rounded border border-slate-700 overflow-hidden text-xs mb-4 self-start">
            <button
              onClick={() => setActiveTab('general')}
              className={`px-3 py-1 ${activeTab === 'general' ? 'bg-indigo-600 text-white' : 'bg-slate-900 text-slate-300 hover:bg-slate-800'}`}
            >
              General
            </button>
            <button
              onClick={() => setActiveTab('provider')}
              className={`px-3 py-1 ${activeTab === 'provider' ? 'bg-indigo-600 text-white' : 'bg-slate-900 text-slate-300 hover:bg-slate-800'}`}
            >
              Provider Params
            </button>
          </div>
        )}

        <div className="space-y-4 overflow-y-auto pr-1 flex-1 min-h-0">
          {(!showProviderTab || activeTab === 'general') && (
            <>
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
                <label className="block text-sm text-slate-400 mb-1">Agents</label>
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

              <div>
                <label className="block text-sm text-slate-400 mb-1">Agent Persona or AGENTS file (optional)</label>
                <input
                  type="file"
                  multiple
                  onChange={handleAgentsSelection}
                  {...({ webkitdirectory: 'true', directory: '' } as any)}
                  className="w-full bg-slate-800 border border-slate-700 rounded px-3 py-2 file:mr-3 file:rounded file:border-0 file:bg-slate-700 file:px-3 file:py-1 file:text-slate-100"
                />
                {agentsMdName ? (
                  <p className="mt-1 text-xs text-slate-500">Selected: {agentsMdName}</p>
                ) : (
                  <p className="mt-1 text-xs text-slate-500">If a single file is selected, it is copied as AGENTS.md. If a directory is selected, only AGENTS.md and skills/ (if present) are copied.</p>
                )}
              </div>
            </>
          )}

          {showProviderTab && activeTab === 'provider' && renderProviderFields()}
        </div>

        <div className="flex justify-end mt-6 space-x-3 shrink-0">
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
