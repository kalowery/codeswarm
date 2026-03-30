'use client'

import { useEffect, useMemo, useRef, useState } from 'react'
import { getBackendHttpOrigin } from '@/lib/runtime'

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
  const [personaPickerMode, setPersonaPickerMode] = useState<'file' | 'directory'>('file')
  const [loading, setLoading] = useState(false)
  const [loadingProviders, setLoadingProviders] = useState(true)
  const [providersError, setProvidersError] = useState<string | null>(null)
  const agentsFileInputRef = useRef<HTMLInputElement | null>(null)
  const agentsDirInputRef = useRef<HTMLInputElement | null>(null)

  const buildProviderDefaults = (provider: LaunchProvider | undefined) => {
    if (!provider) return {}
    const defaults: Record<string, string> = {}
    const panelFields = (provider.launch_panels ?? [])
      .flatMap((panel) => (Array.isArray(panel.fields) ? panel.fields : []))
    const fields = [
      ...(Array.isArray(provider.launch_fields) ? provider.launch_fields : []),
      ...panelFields
    ]
    for (const field of fields) {
      const providerDefault = provider.defaults?.[field.key]
      const fieldDefault = typeof field.default !== 'undefined' ? field.default : providerDefault
      if (typeof fieldDefault === 'undefined' || fieldDefault === null) continue
      defaults[field.key] = String(fieldDefault)
    }
    return defaults
  }

  const applyProviderSelection = (providerId: string) => {
    setSelectedProvider(providerId)
    const provider = providers.find((item) => item.id === providerId)
    setProviderValues(buildProviderDefaults(provider))
  }

  const applyPersonaSelection = (
    rootName: string,
    agentsText: string,
    skillsFiles: AgentsSkillFile[]
  ) => {
    setAgentsMdName(`${rootName}/ (persona directory)`)
    setAgentsMdContent(agentsText)
    setAgentsBundle({
      mode: 'directory',
      agents_md_content: agentsText,
      skills_files: skillsFiles
    })
  }

  const handlePersonaDirectoryPicker = async () => {
    const showDirectoryPickerFn =
      (globalThis as any).showDirectoryPicker ?? (window as any).showDirectoryPicker
    if (typeof showDirectoryPickerFn !== 'function') {
      // Legacy fallback for browsers/environments where showDirectoryPicker is unavailable.
      // handleAgentsSelection still whitelists only AGENTS.md and skills/** payload content.
      agentsDirInputRef.current?.click()
      return
    }

    try {
      const rootHandle = await showDirectoryPickerFn.call(window)
      const rootName = String(rootHandle?.name || 'persona')
      setAgentsMdName(`${rootName}/ (persona directory)`)

      let agentsFileHandle: any
      try {
        agentsFileHandle = await rootHandle.getFileHandle('AGENTS.md')
      } catch {
        alert('Selected directory must contain AGENTS.md at its root.')
        clearAgentsSelection()
        return
      }

      const agentsText = await (await agentsFileHandle.getFile()).text()

      const collectSkills = async (
        dirHandle: any,
        prefix = ''
      ): Promise<AgentsSkillFile[]> => {
        const out: AgentsSkillFile[] = []
        for await (const entry of dirHandle.values()) {
          const name = String(entry?.name || '')
          if (!name) continue
          const rel = prefix ? `${prefix}/${name}` : name
          if (entry.kind === 'file') {
            const content = await (await entry.getFile()).text()
            out.push({ path: rel, content })
          } else if (entry.kind === 'directory') {
            out.push(...(await collectSkills(entry, rel)))
          }
        }
        return out
      }

      let skillsFiles: AgentsSkillFile[] = []
      try {
        const skillsHandle = await rootHandle.getDirectoryHandle('skills')
        skillsFiles = await collectSkills(skillsHandle)
      } catch {
        skillsFiles = []
      }

      applyPersonaSelection(rootName, agentsText, skillsFiles)
    } catch (err: any) {
      const name = String(err?.name || '')
      if (name === 'AbortError') return
      alert('Unable to read selected directory. Ensure AGENTS.md exists at root and try again.')
      clearAgentsSelection()
    }
  }

  useEffect(() => {
    const apiBase = getBackendHttpOrigin()
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
          const nextProviderId =
            selectedProvider && list.some((p) => p.id === selectedProvider)
              ? selectedProvider
              : list[0].id
          setSelectedProvider(nextProviderId)
          setProviderValues(buildProviderDefaults(list.find((p) => p.id === nextProviderId)))
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
    }
  }, [activeProvider])

  async function handleLaunch() {
    const nodeCount = parseInt(nodes, 10)

    if (isNaN(nodeCount) || nodeCount < 1) {
      alert('Agent count must be at least 1')
      return
    }

    try {
      setLoading(true)
      const apiBase = getBackendHttpOrigin()
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
    const fieldTestId = `launch-provider-field-${field.key}`
    if (type === 'boolean') {
      return (
        <label key={field.key} className="flex items-center gap-2 text-sm text-slate-300">
          <input
            data-testid={fieldTestId}
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
            data-testid={fieldTestId}
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
          data-testid={fieldTestId}
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
      e.target.value = ''
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
      e.target.value = ''
      return
    }

    const firstRel = String((files[0] as any).webkitRelativePath || '')
    const rootName = firstRel.split('/')[0]
    if (!rootName) {
      alert('Unable to resolve selected directory.')
      clearAgentsSelection()
      e.target.value = ''
      return
    }

    const agentsPath = `${rootName}/AGENTS.md`
    const agentsFile = files.find((f) => String((f as any).webkitRelativePath || '') === agentsPath)
    if (!agentsFile) {
      alert('Selected directory must contain AGENTS.md at its root.')
      clearAgentsSelection()
      e.target.value = ''
      return
    }

    const rootPrefix = `${rootName}/`
    const skillsPrefix = `${rootName}/skills/`
    const skillEntries: Array<{ file: File; skillRelPath: string }> = []
    for (const file of files) {
      const rel = String((file as any).webkitRelativePath || '')
      if (!rel.startsWith(rootPrefix)) continue
      if (!rel.startsWith(skillsPrefix)) continue
      const skillRelPath = rel.slice(skillsPrefix.length)
      if (!skillRelPath || skillRelPath.endsWith('/')) continue
      skillEntries.push({ file, skillRelPath })
    }

    const agentsText = await agentsFile.text()
    const skillsFiles: AgentsSkillFile[] = await Promise.all(
      skillEntries.map(async ({ file, skillRelPath }) => ({
        path: skillRelPath,
        content: await file.text()
      }))
    )

    applyPersonaSelection(rootName, agentsText, skillsFiles)
    e.target.value = ''
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
    <div data-testid="launch-modal" className="fixed inset-0 z-50 bg-black/75 flex items-center justify-center">
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
              data-testid="launch-provider-select"
              value={selectedProvider}
              onChange={(e) => applyProviderSelection(e.target.value)}
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
              data-testid="launch-general-tab"
              onClick={() => setActiveTab('general')}
              className={`px-3 py-1 ${activeTab === 'general' ? 'bg-indigo-600 text-white' : 'bg-slate-900 text-slate-300 hover:bg-slate-800'}`}
            >
              General
            </button>
            <button
              data-testid="launch-provider-tab"
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
                  data-testid="launch-alias-input"
                  value={alias}
                  onChange={(e) => setAlias(e.target.value)}
                  className="w-full bg-slate-800 border border-slate-700 rounded px-3 py-2"
                  placeholder="optional-name"
                />
              </div>

              <div>
                <label className="block text-sm text-slate-400 mb-1">Agents</label>
                <input
                  data-testid="launch-nodes-input"
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
                  data-testid="launch-system-prompt-input"
                  value={prompt}
                  onChange={(e) => setPrompt(e.target.value)}
                  className="w-full bg-slate-800 border border-slate-700 rounded px-3 py-2 h-24"
                  placeholder="You are a skilled GPU programmer..."
                />
              </div>

              <div>
                <label className="block text-sm text-slate-400 mb-1">Agent Persona or AGENTS file (optional)</label>
                <input
                  ref={agentsFileInputRef}
                  type="file"
                  multiple
                  onChange={handleAgentsSelection}
                  className="hidden"
                />
                <input
                  ref={agentsDirInputRef}
                  type="file"
                  multiple
                  onChange={handleAgentsSelection}
                  {...({ webkitdirectory: 'true', directory: '' } as any)}
                  className="hidden"
                />
                <div className="relative">
                  <div className="mb-2 inline-flex rounded border border-slate-700 overflow-hidden text-xs">
                    <button
                      type="button"
                      onClick={() => setPersonaPickerMode('file')}
                      className={`px-3 py-1 ${personaPickerMode === 'file' ? 'bg-indigo-600 text-white' : 'bg-slate-900 text-slate-300 hover:bg-slate-800'}`}
                    >
                      File
                    </button>
                    <button
                      type="button"
                      onClick={() => setPersonaPickerMode('directory')}
                      className={`px-3 py-1 ${personaPickerMode === 'directory' ? 'bg-indigo-600 text-white' : 'bg-slate-900 text-slate-300 hover:bg-slate-800'}`}
                    >
                      Directory
                    </button>
                  </div>
                  <button
                    type="button"
                    onClick={() => {
                      if (personaPickerMode === 'directory') {
                        void handlePersonaDirectoryPicker()
                      } else {
                        agentsFileInputRef.current?.click()
                      }
                    }}
                    className="w-full bg-slate-800 border border-slate-700 rounded px-3 py-2 text-left"
                  >
                    {agentsMdName
                      ? `Selected: ${agentsMdName}`
                      : personaPickerMode === 'directory'
                        ? 'Choose Persona Directory…'
                        : 'Choose AGENTS File…'}
                  </button>
                </div>
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
            data-testid="launch-cancel-button"
            onClick={onClose}
            className="px-4 py-2 bg-slate-700 rounded"
          >
            Cancel
          </button>
          <button
            data-testid="launch-submit-button"
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
