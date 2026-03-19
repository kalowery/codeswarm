'use client'

import { useEffect, useState, useRef } from 'react'
import { useSwarmStore } from '@/lib/store'
import { useWebSocket } from '@/lib/useWebSocket'
import LaunchModal from '@/components/LaunchModal'
import Image from 'next/image'
import ReactMarkdown from 'react-markdown'
import type { NodeTurn, PendingApproval, TokenUsage } from '@/lib/store'
import remarkGfm from 'remark-gfm'

type SwarmViewMode = 'tabs' | 'grid'

interface GridLayout {
  cols: number
  scale: number
}

const USD_PER_M_INPUT = Number(process.env.NEXT_PUBLIC_INPUT_TOKENS_USD_PER_1M ?? '1.75')
const USD_PER_M_CACHED_INPUT = Number(process.env.NEXT_PUBLIC_CACHED_INPUT_TOKENS_USD_PER_1M ?? '0.175')
const USD_PER_M_OUTPUT = Number(process.env.NEXT_PUBLIC_OUTPUT_TOKENS_USD_PER_1M ?? '14')
const USD_PER_M_REASONING_OUTPUT = Number(process.env.NEXT_PUBLIC_REASONING_OUTPUT_TOKENS_USD_PER_1M ?? '0')

function normalizeMarkdown(content: string, phase: string) {
  if (phase !== 'completed') return content

  const fenceMatches = content.match(/```/g)
  if (!fenceMatches || fenceMatches.length !== 2) return content

  const fencePattern = /^([\s\S]*?)^```markdown[ \t]*\r?\n([\s\S]*?)^```[ \t]*\s*$/m
  const match = content.match(fencePattern)
  if (!match) return content

  return match[2].trim()
}

function estimateUsageUsd(usage: TokenUsage | undefined) {
  if (!usage) return 0

  const inputTokens = Math.max(0, usage.input_tokens ?? 0)
  const cachedInputTokens = Math.max(0, usage.cached_input_tokens ?? 0)
  const nonCachedInputTokens = Math.max(0, inputTokens - cachedInputTokens)
  const outputTokens = Math.max(0, usage.output_tokens ?? 0)
  const reasoningOutputTokens = Math.max(0, usage.reasoning_output_tokens ?? 0)

  return (
    (nonCachedInputTokens / 1_000_000) * USD_PER_M_INPUT +
    (cachedInputTokens / 1_000_000) * USD_PER_M_CACHED_INPUT +
    (outputTokens / 1_000_000) * USD_PER_M_OUTPUT +
    (reasoningOutputTokens / 1_000_000) * USD_PER_M_REASONING_OUTPUT
  )
}

function latestSessionUsage(turns: NodeTurn[]): TokenUsage | undefined {
  let best: TokenUsage | undefined
  for (const turn of turns) {
    if (!turn.usage) continue
    if (!best) {
      best = turn.usage
      continue
    }
    if ((turn.usage.total_tokens ?? 0) >= (best.total_tokens ?? 0)) {
      best = turn.usage
    }
  }
  return best
}

function buildTurnsSignature(turns: NodeTurn[]) {
  if (!turns || turns.length === 0) return '0'
  const last = turns[turns.length - 1]
  const deltasLen = Array.isArray(last.deltas)
    ? last.deltas.reduce((sum, part) => sum + String(part ?? '').length, 0)
    : 0
  const execStdoutLen = last.execution?.stdout ? String(last.execution.stdout).length : 0
  return [
    turns.length,
    last.phase ?? '',
    deltasLen,
    (last.reasoning ?? '').length,
    (last.error ?? '').length,
    last.usage?.total_tokens ?? '',
    execStdoutLen,
    !!last.approval
  ].join(':')
}

function formatUsd(value: number) {
  return value.toLocaleString(undefined, {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: 2,
    maximumFractionDigits: 4
  })
}

type ApprovalChangeDetail = {
  path: string
  summary: string
  diff?: string
}

export default function Home() {
  const swarms = useSwarmStore((s) => s.swarms)
  const setSwarms = useSwarmStore((s) => s.setSwarms)
  const selectSwarm = useSwarmStore((s) => s.selectSwarm)
  const selected = useSwarmStore((s) => s.selectedSwarm)
  const setPendingPrompt = useSwarmStore((s) => s.setPendingPrompt)
  const activeNodeBySwarm = useSwarmStore((s) => s.activeNodeBySwarm)
  const setActiveNode = useSwarmStore((s) => s.setActiveNode)
  const interSwarmQueue = useSwarmStore((s) => s.interSwarmQueue)
  const setInterSwarmQueue = useSwarmStore((s) => s.setInterSwarmQueue)
  const handleMessage = useSwarmStore((s) => s.handleMessage)
  const launchError = useSwarmStore((s) => s.launchError)
  const clearLaunchError = useSwarmStore((s) => s.clearLaunchError)

  const { status: wsStatus } = useWebSocket()

  useEffect(() => {
    const apiBase = `${window.location.protocol}//${window.location.hostname}:4000`

    const fetchJson = async (url: string) => {
      const res = await fetch(url)
      if (!res.ok) {
        throw new Error(`HTTP ${res.status}`)
      }
      return res.json()
    }

    fetchJson(`${apiBase}/swarms`)
      .then((data) => setSwarms(data))
      .catch((err) => {
        console.warn('Failed to fetch swarms:', err)
      })

    fetchJson(`${apiBase}/queue`)
      .then((data) => setInterSwarmQueue(data))
      .catch((err) => {
        console.warn('Failed to fetch queue:', err)
      })
  }, [setSwarms, setInterSwarmQueue])

  const pendingLaunches = useSwarmStore((s) => s.pendingLaunches)
  const swarmList = Object.values(swarms)
  const active = selected ? swarms[selected] : undefined
  const activeIsTerminating = (active?.status ?? '').toLowerCase() === 'terminating'
  const activePendingApprovals = active
    ? (() => {
        const pending = new Map<string, { nodeId: number; approval: PendingApproval }>()
        for (const [nodeIdRaw, approvals] of Object.entries(active.pending_approvals ?? {})) {
          const nodeId = Number(nodeIdRaw)
          for (const approval of approvals ?? []) {
            if (!approval?.call_id) continue
            pending.set(`${nodeId}:${approval.call_id}`, { nodeId, approval })
          }
        }
        return Array.from(pending.values()).sort((a, b) => {
          const ta = Number(a.approval.created_at_ms ?? 0)
          const tb = Number(b.approval.created_at_ms ?? 0)
          if (ta !== tb) return ta - tb
          return a.nodeId - b.nodeId
        })
      })()
    : []

  function getNodeVisualState(swarmId: string, nodeId: number) {
    const swarm = swarms[swarmId]
    if (!swarm) return { attention: false, working: false, ready: false }

    const node = swarm.nodes[nodeId]
    if (!node || node.turns.length === 0) return { attention: false, working: false, ready: true }

    const hasApproval = (swarm.pending_approvals?.[nodeId]?.length ?? 0) > 0
    if (hasApproval) {
      return { attention: true, working: false, ready: false }
    }

    const hasWorking = node.turns.some((t) => t.phase === 'streaming' || t.phase === 'executing')
    if (hasWorking) {
      return { attention: false, working: true, ready: false }
    }

    const last = node.turns[node.turns.length - 1]
    if (last.phase === 'completed') return { attention: false, working: false, ready: true }

    return { attention: false, working: false, ready: false }
  }

  function nodeNeedsAttention(swarmId: string, nodeId: number) {
    return getNodeVisualState(swarmId, nodeId).attention
  }

  function nodeIsWorking(swarmId: string, nodeId: number) {
    return getNodeVisualState(swarmId, nodeId).working
  }

  function nodeIsReady(swarmId: string, nodeId: number) {
    return getNodeVisualState(swarmId, nodeId).ready
  }

  function swarmNeedsAttention(swarmId: string) {
    const swarm = swarms[swarmId]
    if (!swarm) return false
    return Object.keys(swarm.nodes).some((id) =>
      nodeNeedsAttention(swarmId, Number(id))
    )
  }

  function swarmIsReady(swarmId: string) {
    const swarm = swarms[swarmId]
    if (!swarm) return false
    const nodeIds = Object.keys(swarm.nodes).map((id) => Number(id))
    return nodeIds.length > 0 && nodeIds.every((id) => nodeIsReady(swarmId, id))
  }

  function swarmHasWorking(swarmId: string) {
    const swarm = swarms[swarmId]
    if (!swarm) return false
    return Object.keys(swarm.nodes).some((id) => nodeIsWorking(swarmId, Number(id)))
  }

  const [showLaunch, setShowLaunch] = useState(false)
  const [viewModeBySwarm, setViewModeBySwarm] = useState<Record<string, SwarmViewMode>>({})
  const nodeScrollRef = useRef<HTMLDivElement | null>(null)
  const tabsPanelRef = useRef<HTMLDivElement | null>(null)
  const tabsTurnsViewportRef = useRef<HTMLDivElement | null>(null)
  const scrolledApprovalKeyRef = useRef<string>('')
  const turnsSignatureRef = useRef<string>('')
  const pendingUserScrollAcknowledgeRef = useRef(false)
  const programmaticScrollUntilRef = useRef(0)
  const gridViewportRef = useRef<HTMLDivElement | null>(null)
  const gridLayoutRef = useRef<GridLayout>({ cols: 1, scale: 1 })
  const [canScrollLeft, setCanScrollLeft] = useState(false)
  const [canScrollRight, setCanScrollRight] = useState(false)
  const [showUnseenContentBelow, setShowUnseenContentBelow] = useState(false)
  const [gridLayout, setGridLayout] = useState<GridLayout>({ cols: 1, scale: 1 })
  const activeViewMode: SwarmViewMode = active ? (viewModeBySwarm[active.swarm_id] ?? 'tabs') : 'tabs'
  const activeNodeId = active ? (activeNodeBySwarm[active.swarm_id] ?? 0) : 0
  const activeNode = active ? active.nodes[activeNodeId] : undefined
  const activeTurnsSignature = buildTurnsSignature(activeNode?.turns ?? [])

  function hasContentBelowViewport(el: HTMLDivElement) {
    return el.scrollTop + el.clientHeight < el.scrollHeight - 6
  }

  function handleTabsTurnsScroll() {
    const el = tabsTurnsViewportRef.current
    if (!el) return
    const hasBelow = hasContentBelowViewport(el)
    const now = Date.now()
    if (pendingUserScrollAcknowledgeRef.current && now > programmaticScrollUntilRef.current) {
      pendingUserScrollAcknowledgeRef.current = false
      setShowUnseenContentBelow(false)
      return
    }
    if (!hasBelow) {
      setShowUnseenContentBelow(false)
    }
  }

  function updateScrollButtons() {
    const el = nodeScrollRef.current
    if (!el) return
    setCanScrollLeft(el.scrollLeft > 0)
    setCanScrollRight(el.scrollLeft + el.clientWidth < el.scrollWidth - 1)
  }

  function scrollNodeTabs(direction: 'left' | 'right') {
    const el = nodeScrollRef.current
    if (!el) return
    const delta = Math.max(120, Math.floor(el.clientWidth * 0.5))
    el.scrollBy({
      left: direction === 'left' ? -delta : delta,
      behavior: 'smooth'
    })
  }

  useEffect(() => {
    function handleResize() {
      updateScrollButtons()
    }
    window.addEventListener('resize', handleResize)
    setTimeout(updateScrollButtons, 0)
    return () => window.removeEventListener('resize', handleResize)
  }, [active, selected, viewModeBySwarm])

  useEffect(() => {
    if (!active || activeViewMode !== 'grid') return

    const orderedNodeIds = Object.keys(active.nodes)
      .map((id) => Number(id))
      .sort((a, b) => a - b)
    if (orderedNodeIds.length === 0) return

    const isEditableTarget = (target: EventTarget | null) => {
      const el = target as HTMLElement | null
      if (!el) return false
      const tag = el.tagName
      return (
        el.isContentEditable ||
        tag === 'INPUT' ||
        tag === 'TEXTAREA' ||
        tag === 'SELECT'
      )
    }

    const onKeyDown = (e: KeyboardEvent) => {
      if (e.defaultPrevented || isEditableTarget(e.target)) return

      const currentNodeId = activeNodeBySwarm[active.swarm_id] ?? orderedNodeIds[0]
      let currentIdx = orderedNodeIds.indexOf(currentNodeId)
      if (currentIdx < 0) currentIdx = 0

      let nextIdx = currentIdx
      let handled = false
      const cols = Math.max(1, gridLayout.cols)

      if (e.key === 'ArrowRight') {
        nextIdx = Math.min(orderedNodeIds.length - 1, currentIdx + 1)
        handled = true
      } else if (e.key === 'ArrowLeft') {
        nextIdx = Math.max(0, currentIdx - 1)
        handled = true
      } else if (e.key === 'ArrowDown') {
        nextIdx = Math.min(orderedNodeIds.length - 1, currentIdx + cols)
        handled = true
      } else if (e.key === 'ArrowUp') {
        nextIdx = Math.max(0, currentIdx - cols)
        handled = true
      } else if (e.key === 'Tab') {
        nextIdx = e.shiftKey
          ? (currentIdx - 1 + orderedNodeIds.length) % orderedNodeIds.length
          : (currentIdx + 1) % orderedNodeIds.length
        handled = true
      }

      if (!handled) return
      e.preventDefault()

      const nextNodeId = orderedNodeIds[nextIdx]
      if (nextNodeId !== currentNodeId) {
        setActiveNode(active.swarm_id, nextNodeId)
      }
    }

    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [active, activeViewMode, activeNodeBySwarm, gridLayout.cols, setActiveNode])

  const [isSending, setIsSending] = useState(false)
  const [isTerminating, setIsTerminating] = useState(false)
  const [downloadWorkspaceOnTerminate, setDownloadWorkspaceOnTerminate] = useState(false)
  const [dotCount, setDotCount] = useState(0)
  const [approvalSubmitting, setApprovalSubmitting] = useState<Record<string, boolean>>({})

  function computeBestGridLayout(
    width: number,
    height: number,
    nodeCount: number,
    baseWidth: number,
    baseHeight: number,
    gapPx: number
  ): GridLayout {
    if (nodeCount <= 1) return { cols: 1, scale: Math.min(1, width / baseWidth, height / baseHeight) || 1 }

    let bestCols = 1
    let bestScale = 0.05

    for (let cols = 1; cols <= nodeCount; cols += 1) {
      const rows = Math.ceil(nodeCount / cols)
      const usableWidth = Math.max(1, width - Math.max(0, cols - 1) * gapPx)
      const usableHeight = Math.max(1, height - Math.max(0, rows - 1) * gapPx)
      const scaleX = usableWidth / (cols * baseWidth)
      const scaleY = usableHeight / (rows * baseHeight)
      const scale = Math.min(scaleX, scaleY)
      if (scale > bestScale) {
        bestScale = scale
        bestCols = cols
      }
    }

    return {
      cols: bestCols,
      scale: Math.max(0.05, Math.min(1, bestScale))
    }
  }

  useEffect(() => {
    const id = setInterval(() => setDotCount((d) => (d + 1) % 4), 500)
    return () => clearInterval(id)
  }, [])

  useEffect(() => {
    if (!active || activeViewMode !== 'grid') return

    const baseWidth = 420
    const baseHeight = 280
    const gapPx = 8
    const el = gridViewportRef.current
    if (!el) return

    const recalc = () => {
      const width = Math.max(1, el.clientWidth - 8)
      const height = Math.max(1, el.clientHeight - 8)
      const next = computeBestGridLayout(width, height, active.node_count, baseWidth, baseHeight, gapPx)
      const prev = gridLayoutRef.current
      const sameCols = prev.cols === next.cols
      const sameScale = Math.abs(prev.scale - next.scale) < 0.002
      if (sameCols && sameScale) return
      gridLayoutRef.current = next
      setGridLayout(next)
    }

    recalc()

    const observer = new ResizeObserver(recalc)
    observer.observe(el)
    window.addEventListener('resize', recalc)
    return () => {
      observer.disconnect()
      window.removeEventListener('resize', recalc)
    }
  }, [active?.swarm_id, active?.node_count, activeViewMode])

  useEffect(() => {
    if (!active || activeViewMode !== 'tabs') return
    const activeNodeId = activeNodeBySwarm[active.swarm_id] ?? 0
    const activeNode = active.nodes[activeNodeId]
    if (!activeNode) return

    const pendingApproval = (active.pending_approvals?.[activeNodeId] ?? [])[0]
    if (!pendingApproval) {
      scrolledApprovalKeyRef.current = ''
      return
    }
    const approvalIdentity = pendingApproval.call_id || pendingApproval.injection_id || ''
    const approvalKey = `${active.swarm_id}:${activeNodeId}:${approvalIdentity}`
    if (scrolledApprovalKeyRef.current === approvalKey) return

    const panel = tabsTurnsViewportRef.current
    if (!panel) return

    const raf = requestAnimationFrame(() => {
      const el = panel.querySelector('[data-awaiting-approval="true"]') as HTMLElement | null
      if (el) {
        scrolledApprovalKeyRef.current = approvalKey
        programmaticScrollUntilRef.current = Date.now() + 1500
        el.scrollIntoView({ block: 'center', behavior: 'smooth' })
      }
    })

    return () => cancelAnimationFrame(raf)
  }, [active, activeViewMode, activeNodeBySwarm])

  useEffect(() => {
    if (!active || activeViewMode !== 'tabs') {
      setShowUnseenContentBelow(false)
      pendingUserScrollAcknowledgeRef.current = false
      turnsSignatureRef.current = ''
      return
    }

    if (turnsSignatureRef.current === activeTurnsSignature) return
    turnsSignatureRef.current = activeTurnsSignature

    const raf = requestAnimationFrame(() => {
      const el = tabsTurnsViewportRef.current
      if (!el) return
      if (hasContentBelowViewport(el)) {
        pendingUserScrollAcknowledgeRef.current = true
        setShowUnseenContentBelow(true)
      } else {
        setShowUnseenContentBelow(false)
      }
    })

    return () => cancelAnimationFrame(raf)
  }, [active?.swarm_id, activeViewMode, activeNodeId, activeTurnsSignature])

  async function sendApproval(
    job_id: string,
    call_id: string,
    approved: boolean,
    decision?: unknown,
    node_id?: number,
    injection_id?: string
  ) {
    const apiBase = `${window.location.protocol}//${window.location.hostname}:4000`
    const res = await fetch(`${apiBase}/approval`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        job_id,
        call_id,
        node_id,
        injection_id,
        approved,
        decision
      })
    })
    if (!res.ok) {
      const payload = await res.json().catch(() => null)
      // Approval submit is best-effort; backend can return non-2xx while
      // request is still in flight/racing with ack. Keep UI responsive.
      console.warn('Approval submit non-OK response', {
        status: res.status,
        job_id,
        call_id,
        approved,
        error: payload?.error
      })
    }

    // Force an immediate approval-state refresh after each submit so
    // missed websocket events cannot leave stale approval panels.
    fetch(`${apiBase}/approvals`)
      .then((snapshotRes) => snapshotRes.json())
      .then((data) => handleMessage({ type: 'approvals_snapshot', payload: data }))
      .catch(() => {})
  }

  async function withApprovalSubmit(callId: string, action: () => Promise<void>) {
    if (!callId) return
    if (approvalSubmitting[callId]) return
    setApprovalSubmitting((prev) => ({ ...prev, [callId]: true }))
    try {
      await action()
    } catch (err) {
      console.error('Approval submit failed', { call_id: callId, error: err })
    } finally {
      setTimeout(() => {
        setApprovalSubmitting((prev) => {
          const next = { ...prev }
          delete next[callId]
          return next
        })
      }, 3000)
    }
  }

  function approvalHasPolicyOption(availableDecisions: Array<string | Record<string, any>> | undefined) {
    if (!Array.isArray(availableDecisions)) return false
    return availableDecisions.some(
      (d) =>
        typeof d === 'object' &&
        d !== null &&
        (
          'approved_execpolicy_amendment' in d ||
          'acceptWithExecpolicyAmendment' in d
        )
    )
  }

  function isCompactPolicyAmendment(amendment: string[] | undefined) {
    if (!Array.isArray(amendment) || amendment.length === 0) return false
    if (amendment.length > 8) return false
    let total = 0
    for (const part of amendment) {
      if (typeof part !== 'string') return false
      if (part.includes('\n') || part.includes('\r')) return false
      if (part.length > 200) return false
      total += part.length
      if (total > 400) return false
    }
    return true
  }

  function buildPolicyDecision(
    availableDecisions: Array<string | Record<string, any>> | undefined,
    amendment: string[]
  ) {
    if (!isCompactPolicyAmendment(amendment)) {
      return approveToken(availableDecisions)
    }

    const hasAcceptStyle = Array.isArray(availableDecisions) &&
      availableDecisions.some(
        (d) =>
          typeof d === 'object' &&
          d !== null &&
          'acceptWithExecpolicyAmendment' in d
      )

    if (hasAcceptStyle) {
      return {
        acceptWithExecpolicyAmendment: {
          execpolicy_amendment: amendment
        }
      }
    }

    return {
      approved_execpolicy_amendment: {
        proposed_execpolicy_amendment: amendment
      }
    }
  }

  function approveToken(availableDecisions: Array<string | Record<string, any>> | undefined) {
    return Array.isArray(availableDecisions) && availableDecisions.includes('accept')
      ? 'accept'
      : 'approved'
  }

  function denyToken(availableDecisions: Array<string | Record<string, any>> | undefined) {
    return Array.isArray(availableDecisions) && availableDecisions.includes('cancel')
      ? 'cancel'
      : 'abort'
  }

  function formatPolicyRule(rule: string[] | undefined) {
    if (!Array.isArray(rule) || rule.length === 0) return 'N/A'
    return rule.join(' ')
  }

  function approvalIsBusy(approval: PendingApproval) {
    return (
      approval.status === 'submitted' ||
      approval.status === 'acknowledged' ||
      approval.status === 'started' ||
      approval.status === 'resolved' ||
      approval.status === 'rejected' ||
      approval.status === 'timeout'
    )
  }

  function formatApprovalCommand(command: PendingApproval['command']) {
    if (Array.isArray(command)) return command.join(' ')
    if (typeof command === 'string') return command
    if (command && typeof command === 'object') {
      const anyCmd = command as Record<string, any>
      const collectChangeEntries = (value: unknown): Array<Record<string, unknown>> => {
        const out: Array<Record<string, unknown>> = []
        const visit = (node: unknown) => {
          if (!node) return
          if (Array.isArray(node)) {
            for (const item of node) visit(item)
            return
          }
          if (typeof node !== 'object') return
          const rec = node as Record<string, unknown>
          if (
            typeof rec.path === 'string' ||
            typeof rec.file === 'string' ||
            typeof rec.target === 'string' ||
            typeof rec.new_path === 'string' ||
            typeof rec.old_path === 'string' ||
            typeof rec.from === 'string' ||
            typeof rec.to === 'string'
          ) {
            out.push(rec)
          }
          for (const value of Object.values(rec)) {
            if (value && (Array.isArray(value) || typeof value === 'object')) visit(value)
          }
        }
        visit(value)
        return out
      }
      if (anyCmd.type === 'file_changes' || anyCmd.type === 'file_changes_apply') {
        const changes = anyCmd.changes
        const changeEntries = collectChangeEntries(changes)
        if (changeEntries.length > 0) return `apply ${changeEntries.length} file change(s)`
        if (Array.isArray(changes)) return `apply ${changes.length} file change(s)`
        if (changes && typeof changes === 'object') return 'apply file changes'
        return 'apply file changes'
      }
      const changes = anyCmd.changes
      const changeEntries = collectChangeEntries(changes)
      if (changeEntries.length > 0) return `apply ${changeEntries.length} file change(s)`
      if (changes && typeof changes === 'object') return 'apply file changes'
      try {
        return JSON.stringify(command)
      } catch {
        return '[object command]'
      }
    }
    return String(command ?? '')
  }

  function extractApprovalChangeDetails(command: PendingApproval['command']): ApprovalChangeDetail[] {
    const details = new Map<string, ApprovalChangeDetail>()

    const inferPath = (rec: Record<string, unknown>) => {
      const candidates = [
        rec.path,
        rec.file,
        rec.target,
        rec.new_path,
        rec.old_path,
        rec.from,
        rec.to
      ]
      for (const candidate of candidates) {
        if (typeof candidate === 'string' && candidate.trim()) return candidate.trim()
      }
      return ''
    }

    const inferSummary = (rec: Record<string, unknown>) => {
      const kind = rec.kind
      if (kind && typeof kind === 'object') {
        const kindType = String((kind as Record<string, unknown>).type ?? '').trim()
        if (kindType) return kindType
      }
      for (const key of ['op', 'action', 'type', 'status']) {
        const value = rec[key]
        if (typeof value === 'string' && value.trim()) return value.trim()
      }
      return 'update'
    }

    const visit = (node: unknown) => {
      if (!node) return
      if (Array.isArray(node)) {
        for (const item of node) visit(item)
        return
      }
      if (typeof node !== 'object') return
      const rec = node as Record<string, unknown>
      const path = inferPath(rec)
      if (path) {
        const summary = inferSummary(rec)
        const diff = typeof rec.diff === 'string' && rec.diff.trim() ? rec.diff.trim() : undefined
        details.set(path, { path, summary, diff })
      }
      for (const value of Object.values(rec)) {
        if (value && (Array.isArray(value) || typeof value === 'object')) visit(value)
      }
    }

    if (command && typeof command === 'object') {
      visit(command)
    }

    return Array.from(details.values())
  }

  function extractApprovalFilePaths(command: PendingApproval['command']) {
    return extractApprovalChangeDetails(command).map((detail) => detail.path)
  }

  function renderApprovalChangeSummary(command: PendingApproval['command']) {
    const changes = extractApprovalChangeDetails(command)
    if (changes.length === 0) return null

    return (
      <div className="mt-2 space-y-2">
        <div className="text-[11px] text-slate-300">
          Files: {changes.map((change) => change.path).join(', ')}
        </div>
        <div className="space-y-2">
          {changes.map((change) => {
            const diffLines = change.diff ? change.diff.split(/\r?\n/).slice(0, 8) : []
            const hasMore = change.diff ? change.diff.split(/\r?\n/).length > diffLines.length : false
            return (
              <div key={change.path} className="rounded border border-slate-700 bg-slate-950 p-2">
                <div className="text-[11px] text-amber-200 break-words">
                  {change.summary}: {change.path}
                </div>
                {diffLines.length > 0 && (
                  <pre className="mt-1 whitespace-pre-wrap break-words text-[10px] text-slate-300">
                    {diffLines.join('\n')}
                    {hasMore ? '\n...' : ''}
                  </pre>
                )}
              </div>
            )
          })}
        </div>
      </div>
    )
  }

  function renderFallbackApprovals(
    pendingApprovals: PendingApproval[] | undefined,
    turnCallIds: Set<string>,
    jobId: string,
    nodeId: number,
    knownExecPolicies: string[][] | undefined
  ) {
    const approvals = (pendingApprovals ?? []).filter((a) => !turnCallIds.has(a.call_id))
    if (approvals.length === 0) return null

    return (
      <div className="space-y-2">
        {approvals.map((approval) => (
          <div key={`fallback-${approval.call_id}`} data-awaiting-approval="true" className="text-xs bg-amber-900 border border-amber-500 rounded p-2">
            <div className="text-amber-300 mb-1">Execution approval required</div>
            <div className="text-slate-200">
              $ {formatApprovalCommand(approval.command)}
            </div>
            {renderApprovalChangeSummary(approval.command)}
            <div className="mt-1 text-slate-300">{approval.reason}</div>
            {approval.status && (
              <div className="mt-1 text-[11px] text-amber-200">Status: {approval.status}</div>
            )}
            <div className="mt-2 flex flex-wrap items-center gap-2">
              <button
                disabled={!!approvalSubmitting[approval.call_id] || approvalIsBusy(approval)}
                className="px-2 py-1 bg-emerald-700 rounded text-xs hover:bg-emerald-600 disabled:opacity-60 disabled:cursor-not-allowed"
                onClick={async () => {
                  await withApprovalSubmit(approval.call_id, () =>
                    sendApproval(
                      jobId,
                      approval.call_id,
                      true,
                      approveToken(approval.available_decisions),
                      nodeId,
                      approval.injection_id
                    )
                  )
                }}
              >
                Approve
              </button>
              {Array.isArray(approval.proposed_execpolicy_amendment) &&
                approval.proposed_execpolicy_amendment.length > 0 &&
                approvalHasPolicyOption(approval.available_decisions) &&
                isCompactPolicyAmendment(approval.proposed_execpolicy_amendment) && (
                  <button
                    disabled={!!approvalSubmitting[approval.call_id] || approvalIsBusy(approval)}
                    className="px-2 py-1 bg-emerald-600 rounded text-xs hover:bg-emerald-500 disabled:opacity-60 disabled:cursor-not-allowed"
                    onClick={async () => {
                      await withApprovalSubmit(approval.call_id, () =>
                        sendApproval(
                          jobId,
                          approval.call_id,
                          true,
                          buildPolicyDecision(
                            approval.available_decisions,
                            approval.proposed_execpolicy_amendment as string[]
                          ),
                          nodeId,
                          approval.injection_id
                        )
                      )
                    }}
                  >
                    Approve + Remember
                  </button>
                )}
              <button
                disabled={!!approvalSubmitting[approval.call_id] || approvalIsBusy(approval)}
                className="px-2 py-1 bg-rose-600 rounded text-xs hover:bg-rose-500 disabled:opacity-60 disabled:cursor-not-allowed"
                onClick={async () => {
                  await withApprovalSubmit(approval.call_id, () =>
                    sendApproval(
                      jobId,
                      approval.call_id,
                      false,
                      denyToken(approval.available_decisions),
                      nodeId,
                      approval.injection_id
                    )
                  )
                }}
              >
                Deny
              </button>
            </div>
            {Array.isArray(approval.proposed_execpolicy_amendment) &&
              approval.proposed_execpolicy_amendment.length > 0 && (
                <div className="mt-2 space-y-1 text-slate-200">
                  <div className="font-medium text-amber-200">Proposed one-time policy rule</div>
                  <div className="font-mono text-[11px] text-slate-200 break-words">
                    {formatPolicyRule(approval.proposed_execpolicy_amendment)}
                  </div>
                </div>
              )}
            {Array.isArray(knownExecPolicies) && knownExecPolicies.length > 0 && (
              <div className="mt-2 space-y-1 text-slate-200">
                <div className="font-medium text-amber-200">Known execution policy rules</div>
                <div className="space-y-1">
                  {knownExecPolicies.map((rule, ruleIdx) => (
                    <div key={`${approval.call_id}-${ruleIdx}-${rule.join(' ')}`} className="font-mono text-[11px] text-slate-200 break-words">
                      {formatPolicyRule(rule)}
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        ))}
      </div>
    )
  }

  function renderTurns(
    turns: NodeTurn[],
    pendingApprovals: PendingApproval[] | undefined,
    jobId: string,
    nodeId: number,
    knownExecPolicies: string[][] | undefined
  ) {
    const turnCallIds = new Set<string>()
    return (
      <div className="space-y-4">
        {turns.map((turn) => {
          const turnKey = turn.injection_id
          return (
          <div key={turnKey} className="space-y-2">
            {turn.prompt && (
              <div className="flex justify-end">
                <div className="max-w-[75%] bg-indigo-600 text-white px-3 py-2 rounded-lg rounded-br-sm break-words overflow-hidden">
                  {turn.prompt}
                </div>
              </div>
            )}

            <div className="flex justify-start">
              <div className="max-w-[75%] bg-slate-800 border border-slate-700 px-3 py-2 rounded-lg rounded-bl-sm space-y-2 overflow-hidden">
                {turn.phase !== 'completed' && turn.phase !== 'error' && (
                  <div className={`flex items-center gap-2 text-[10px] ${turn.phase === 'awaiting_approval' ? 'text-amber-400' : 'text-rose-400'}`}>
                    <span className={`inline-block w-2 h-2 rounded-full animate-pulse ${turn.phase === 'awaiting_approval' ? 'bg-amber-400' : 'bg-rose-500'}`} />
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
                  const raw = turn.deltas.join('')
                  if (!raw.trim()) return null

                  if (turn.phase !== 'completed') {
                    return (
                      <div className="markdown-content break-words overflow-x-auto text-sm leading-relaxed" style={{ overflowWrap: 'break-word', wordBreak: 'normal' }}>
                        <ReactMarkdown remarkPlugins={[remarkGfm]}>
                          {raw}
                        </ReactMarkdown>
                      </div>
                    )
                  }

                  const formatted = normalizeMarkdown(raw, turn.phase)
                  const showRaw = raw !== formatted

                  return (
                    <div className="markdown-content break-words overflow-x-auto text-sm leading-relaxed space-y-2" style={{ overflowWrap: 'break-word', wordBreak: 'normal' }}>
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
                    Tokens: {turn.usage.total_tokens}
                    {typeof turn.usage.input_tokens === 'number' && typeof turn.usage.output_tokens === 'number'
                      ? ` (in ${turn.usage.input_tokens}, out ${turn.usage.output_tokens})`
                      : ''}
                    {` · Est: ${formatUsd(estimateUsageUsd(turn.usage))}`}
                  </div>
                )}
              </div>
            </div>
          </div>
          )
        })}
        {renderFallbackApprovals(pendingApprovals, turnCallIds, jobId, nodeId, knownExecPolicies)}
      </div>
    )
  }

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
              {launch.message && (
                <div className="text-xs text-slate-400 mt-1 whitespace-pre-wrap break-words max-h-24 overflow-y-auto">
                  {launch.message}
                </div>
              )}
              {(launch.provider_id || launch.provider || launch.stage) && (
                <div className="text-[11px] text-slate-500 mt-1">
                  {[launch.provider_id || launch.provider, launch.stage].filter(Boolean).join(' · ')}
                </div>
              )}
            </div>
          ))}

          {swarmList.map((swarm) => (
            (() => {
              const swarmSessionCost = Object.values(swarm.nodes).reduce((sum, node) => {
                return sum + estimateUsageUsd(latestSessionUsage(node.turns))
              }, 0)
              const swarmIsTerminating = (swarm.status ?? '').toLowerCase() === 'terminating'

              return (
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
                  {!swarmNeedsAttention(swarm.swarm_id) && swarmHasWorking(swarm.swarm_id) && (
                    <span className="absolute top-2 right-2 w-2 h-2 rounded-full bg-rose-500 animate-pulse" />
                  )}
                  {!swarmNeedsAttention(swarm.swarm_id) && !swarmHasWorking(swarm.swarm_id) && swarmIsReady(swarm.swarm_id) && (
                    <span className="absolute top-2 right-2 w-2 h-2 rounded-full bg-emerald-400" />
                  )}
                  {swarmIsTerminating && (
                    <span className="absolute top-2 right-2 px-2 py-0.5 rounded-full bg-amber-500/20 text-amber-300 text-[10px] border border-amber-500/40 uppercase tracking-wide animate-pulse">
                      Shutting down
                    </span>
                  )}
                  <div className="font-medium">{swarm.alias}</div>
                  <div className="text-sm text-slate-400">
                  {(swarm.status ?? 'unknown').toUpperCase()} · {swarm.node_count} agent{swarm.node_count === 1 ? '' : 's'}
                </div>
                  {swarmIsTerminating && swarm.termination_message && (
                    <div className="text-xs text-amber-300 mt-1 whitespace-pre-wrap break-words max-h-20 overflow-y-auto">
                      {swarm.termination_message}
                    </div>
                  )}
                  {(swarm.provider_id || swarm.provider) && (
                    <div className="text-xs text-slate-500 mt-1">
                      Provider: {swarm.provider_id || swarm.provider}
                    </div>
                  )}
                  <div className="text-xs text-slate-500 mt-1">
                    Est. spend: {formatUsd(swarmSessionCost)}
                  </div>
                </div>
              )
            })()
          ))}
          {swarmList.length === 0 && (
            <div className="text-slate-500 text-sm">No active swarms</div>
          )}

          <div className="pt-3 mt-3 border-t border-slate-800">
            <div className="text-xs uppercase tracking-wide text-slate-500 mb-2">
              Queued Cross-Swarm Work ({interSwarmQueue.length})
            </div>
            {interSwarmQueue.length === 0 ? (
              <div className="text-xs text-slate-600">No queued inter-swarm prompts.</div>
            ) : (
              <div className="space-y-2 max-h-52 overflow-y-auto pr-1">
                {interSwarmQueue.map((item) => {
                  const sourceAlias = item.source_swarm_id && swarms[item.source_swarm_id]
                    ? swarms[item.source_swarm_id].alias
                    : item.source_swarm_id ?? 'unknown'
                  const targetAlias = item.target_swarm_id && swarms[item.target_swarm_id]
                    ? swarms[item.target_swarm_id].alias
                    : item.target_swarm_id
                  const ageSec = item.created_at
                    ? Math.max(0, Math.floor(Date.now() / 1000 - item.created_at))
                    : 0
                  return (
                    <div key={item.queue_id} className="p-2 rounded bg-slate-900 border border-slate-800 text-xs">
                      <div className="text-slate-300">
                        {sourceAlias} {'->'} {targetAlias}
                      </div>
                      <div className="text-slate-500">
                        selector={(item.selector === 'nodes' ? 'agents' : (item.selector ?? 'idle'))} · age={ageSec}s
                      </div>
                      {item.content && (
                        <div className="text-slate-400 truncate">{item.content}</div>
                      )}
                    </div>
                  )
                })}
              </div>
            )}
          </div>
        </div>
      </div>

      <div className="flex-1 min-w-0 p-6">
        {!active && <div className="text-slate-500">Select a swarm to view details</div>}

        {active && (
          <div>
            {(() => {
              const agentSessionCost = Object.values(active.nodes).reduce((sum, node) => {
                return sum + estimateUsageUsd(latestSessionUsage(node.turns))
              }, 0)
              return (
                <div className="mb-2 text-xs text-slate-400">
                  Estimated spend (swarm session): {formatUsd(agentSessionCost)}
                </div>
              )
            })()}
            <div className="flex items-center justify-between mb-4">
              <div>
                <h1 className="text-xl font-semibold">{active.alias}</h1>
                <div className="text-sm text-slate-400">
                  Status: {active.status ?? 'unknown'}
                  {active.slurm_state
                    ? ` · ${(active.provider ?? '').toLowerCase() === 'slurm' ? 'Slurm' : 'Backend'}: ${active.slurm_state}`
                    : ''}
                </div>
                {activeIsTerminating && (
                  <div className="mt-2 inline-flex items-center gap-2 px-2 py-1 rounded border border-amber-500/40 bg-amber-500/10 text-amber-300 text-xs">
                    <span className="inline-block w-2 h-2 rounded-full bg-amber-400 animate-pulse" />
                    {active.termination_message || 'Swarm shutdown in progress'}
                  </div>
                )}
              </div>
              <button
                disabled={isTerminating || activeIsTerminating}
                onClick={async () => {
                  if (isTerminating || activeIsTerminating) return
                  if (!confirm(`Terminate ${active.alias}? This cannot be undone.`)) return
                  try {
                    setIsTerminating(true)
                    const apiBase = `${window.location.protocol}//${window.location.hostname}:4000`
                    await fetch(`${apiBase}/terminate/${active.alias}`, {
                      method: 'POST',
                      headers: { 'Content-Type': 'application/json' },
                      body: JSON.stringify({
                        download_workspaces_on_shutdown: downloadWorkspaceOnTerminate
                      })
                    })
                  } finally {
                    setIsTerminating(false)
                  }
                }}
                className={`px-3 py-1 rounded text-sm ${
                  (isTerminating || activeIsTerminating)
                    ? 'bg-slate-700 text-slate-400 cursor-not-allowed'
                    : 'bg-rose-600 hover:bg-rose-500'
                }`}
              >
                {(isTerminating || activeIsTerminating) ? 'Terminating…' : 'Terminate'}
              </button>
            </div>
            <label className="mb-3 inline-flex items-center gap-2 text-xs text-slate-400">
              <input
                type="checkbox"
                checked={downloadWorkspaceOnTerminate}
                onChange={(e) => setDownloadWorkspaceOnTerminate(e.target.checked)}
              />
              Download workspace archive on terminate
            </label>

            <div className="mb-3 flex items-center justify-between">
              <div className="text-xs text-slate-500">
                Agents ({Object.keys(active.nodes).length})
              </div>
              <div className="inline-flex rounded border border-slate-700 overflow-hidden text-xs">
                <button
                  onClick={() => setViewModeBySwarm((prev) => ({ ...prev, [active.swarm_id]: 'tabs' }))}
                  className={`px-3 py-1 ${activeViewMode === 'tabs' ? 'bg-indigo-600 text-white' : 'bg-slate-900 text-slate-300 hover:bg-slate-800'}`}
                >
                  Tabs
                </button>
                <button
                  onClick={() => setViewModeBySwarm((prev) => ({ ...prev, [active.swarm_id]: 'grid' }))}
                  className={`px-3 py-1 ${activeViewMode === 'grid' ? 'bg-indigo-600 text-white' : 'bg-slate-900 text-slate-300 hover:bg-slate-800'}`}
                >
                  Grid
                </button>
              </div>
            </div>

            {activePendingApprovals.length > 0 && (
              <div className="mb-3 rounded border border-amber-500 bg-amber-950/50 p-2 space-y-2">
                <div className="text-xs text-amber-200 font-medium">
                  Pending approvals: {activePendingApprovals.length}
                </div>
                <div className="max-h-40 overflow-y-auto space-y-2 pr-1">
                  {activePendingApprovals.map(({ nodeId, approval }) => (
                    <div key={`pending-${nodeId}-${approval.call_id}`} className="rounded border border-amber-700 bg-slate-900 p-2 text-xs">
                      <div className="text-amber-300 mb-1">Agent {nodeId}</div>
                      <div className="text-slate-200 break-words">
                        $ {formatApprovalCommand(approval.command)}
                      </div>
                      {renderApprovalChangeSummary(approval.command)}
                      <div className="mt-1 text-slate-300">{approval.reason}</div>
                      {approval.status && (
                        <div className="mt-1 text-[11px] text-amber-200">Status: {approval.status}</div>
                      )}
                      <div className="mt-2 flex flex-wrap gap-2">
                        <button
                          disabled={!!approvalSubmitting[approval.call_id] || approvalIsBusy(approval)}
                          className="px-2 py-1 bg-emerald-700 rounded text-xs hover:bg-emerald-600 disabled:opacity-60 disabled:cursor-not-allowed"
                          onClick={async () => {
                            await withApprovalSubmit(approval.call_id, () =>
                              sendApproval(
                                active.job_id,
                                approval.call_id,
                                true,
                                approveToken(approval.available_decisions),
                                nodeId,
                                approval.injection_id
                              )
                            )
                          }}
                        >
                          Approve
                        </button>
                        {Array.isArray(approval.proposed_execpolicy_amendment) &&
                          approval.proposed_execpolicy_amendment.length > 0 &&
                          approvalHasPolicyOption(approval.available_decisions) &&
                          isCompactPolicyAmendment(approval.proposed_execpolicy_amendment) && (
                            <button
                              disabled={!!approvalSubmitting[approval.call_id] || approvalIsBusy(approval)}
                              className="px-2 py-1 bg-emerald-600 rounded text-xs hover:bg-emerald-500 disabled:opacity-60 disabled:cursor-not-allowed"
                              onClick={async () => {
                                await withApprovalSubmit(approval.call_id, () =>
                                sendApproval(
                                  active.job_id,
                                  approval.call_id,
                                  true,
                                  buildPolicyDecision(
                                    approval.available_decisions,
                                    approval.proposed_execpolicy_amendment as string[]
                                  ),
                                  nodeId,
                                  approval.injection_id
                                )
                              )
                              }}
                            >
                              Approve + Remember
                            </button>
                          )}
                        <button
                          disabled={!!approvalSubmitting[approval.call_id] || approvalIsBusy(approval)}
                          className="px-2 py-1 bg-rose-600 rounded text-xs hover:bg-rose-500 disabled:opacity-60 disabled:cursor-not-allowed"
                          onClick={async () => {
                            await withApprovalSubmit(approval.call_id, () =>
                              sendApproval(
                                active.job_id,
                                approval.call_id,
                                false,
                                denyToken(approval.available_decisions),
                                nodeId,
                                approval.injection_id
                              )
                            )
                          }}
                        >
                          Deny
                        </button>
                        <button
                          className="px-2 py-1 bg-slate-700 rounded text-xs hover:bg-slate-600"
                          onClick={() => setActiveNode(active.swarm_id, nodeId)}
                        >
                          Focus Agent
                        </button>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {(() => {
              const activeNodeId = activeNodeBySwarm[active.swarm_id] ?? 0
              const activeNode = active.nodes[activeNodeId]
              const orderedNodeIds = Object.keys(active.nodes).map((id) => Number(id)).sort((a, b) => a - b)
              const baseWidth = 420
              const baseHeight = 280
              const tileWidth = Math.max(10, Math.floor(baseWidth * gridLayout.scale))
              const tileHeight = Math.max(10, Math.floor(baseHeight * gridLayout.scale))

              if (activeViewMode === 'tabs') {
                return (
                  <div ref={tabsPanelRef} className="relative bg-slate-900 border border-slate-800 rounded p-4 h-[400px] text-sm flex flex-col min-h-0">
                    <div className="mb-3 border-b border-slate-800 pb-3">
                      <div className="flex items-center gap-2">
                        {canScrollLeft && (
                          <button
                            className="shrink-0 h-7 w-7 rounded border border-slate-700 bg-slate-900 text-slate-300 hover:bg-slate-800"
                            onClick={() => scrollNodeTabs('left')}
                            aria-label="Scroll agents left"
                          >
                            &lt;
                          </button>
                        )}
                        <div
                          ref={nodeScrollRef}
                          onScroll={updateScrollButtons}
                          className="flex flex-nowrap gap-2 overflow-x-auto w-full"
                        >
                        {orderedNodeIds.map((id) => {
                          const isActive = id === activeNodeId
                          const needsAttention = nodeNeedsAttention(active.swarm_id, id)
                          const isWorking = nodeIsWorking(active.swarm_id, id)
                          const isReady = nodeIsReady(active.swarm_id, id)
                          const nodeSessionUsage = latestSessionUsage(active.nodes[id]?.turns ?? [])
                          const nodeSessionCost = estimateUsageUsd(nodeSessionUsage)

                          return (
                            <button
                              key={id}
                              onClick={() => setActiveNode(active.swarm_id, id)}
                              onFocus={() => setActiveNode(active.swarm_id, id)}
                              className={`relative min-w-[72px] shrink-0 px-3 py-2 text-xs rounded-t-md transition border-b-2 ${
                                isActive
                                  ? 'bg-slate-800 text-white border-indigo-500'
                                  : 'bg-slate-900 text-slate-400 border-slate-700 hover:bg-slate-800'
                              }`}
                            >
                              {needsAttention && (
                                <span className="absolute top-1 right-1 w-2 h-2 rounded-full bg-amber-400 animate-pulse" />
                              )}
                              {!needsAttention && isReady && (
                                <span className="absolute top-1 right-1 w-2 h-2 rounded-full bg-emerald-400" />
                              )}
                              {isWorking && !isActive && (
                                <span className="absolute bottom-1 left-1 w-2 h-2 rounded-full bg-rose-500 animate-pulse" />
                              )}
                              <div>Agent {id}</div>
                              <div className="text-[10px] text-slate-500">
                                {formatUsd(nodeSessionCost)}
                              </div>
                            </button>
                          )
                        })}
                        </div>
                        {canScrollRight && (
                          <button
                            className="shrink-0 h-7 w-7 rounded border border-slate-700 bg-slate-900 text-slate-300 hover:bg-slate-800"
                            onClick={() => scrollNodeTabs('right')}
                            aria-label="Scroll agents right"
                          >
                            &gt;
                          </button>
                        )}
                      </div>
                    </div>

                    <div
                      ref={tabsTurnsViewportRef}
                      onScroll={handleTabsTurnsScroll}
                      className="relative flex-1 min-h-0 overflow-y-auto pr-1"
                    >
                      {renderTurns(
                        activeNode.turns,
                        active.pending_approvals?.[activeNodeId],
                        active.job_id,
                        activeNodeId,
                        active.known_exec_policies
                      )}
                    </div>
                    {showUnseenContentBelow && (
                      <button
                        className="absolute bottom-3 left-1/2 -translate-x-1/2 rounded-full border border-indigo-500 bg-slate-900/95 px-3 py-1 text-xs text-indigo-300 shadow hover:bg-slate-800"
                        onClick={() => {
                          const el = tabsTurnsViewportRef.current
                          pendingUserScrollAcknowledgeRef.current = false
                          setShowUnseenContentBelow(false)
                          if (!el) return
                          el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' })
                        }}
                      >
                        New content below
                      </button>
                    )}
                  </div>
                )
              }

              return (
                <div ref={gridViewportRef} className="bg-slate-900 border border-slate-800 rounded p-2 h-[calc(100vh-320px)] min-h-[320px] overflow-hidden">
                  <div
                    className="grid place-content-center gap-2 h-full"
                    style={{
                      gridTemplateColumns: `repeat(${gridLayout.cols}, ${tileWidth}px)`,
                      gridAutoRows: `${tileHeight}px`
                    }}
                  >
                    {orderedNodeIds.map((id) => {
                      const isActive = id === activeNodeId
                      const needsAttention = nodeNeedsAttention(active.swarm_id, id)
                      const node = active.nodes[id]
                      const isWorking = nodeIsWorking(active.swarm_id, id)
                      const isReady = nodeIsReady(active.swarm_id, id)
                      const nodeSessionUsage = latestSessionUsage(node.turns)
                      const nodeSessionCost = estimateUsageUsd(nodeSessionUsage)

                      return (
                        <div
                          key={id}
                          className={`relative overflow-hidden rounded border text-left ${
                            isActive ? 'border-indigo-500 bg-slate-900' : 'border-slate-700 bg-slate-950'
                          }`}
                          style={{ width: tileWidth, height: tileHeight }}
                          role="button"
                          tabIndex={0}
                          onClick={() => {
                            setActiveNode(active.swarm_id, id)
                            setViewModeBySwarm((prev) => ({ ...prev, [active.swarm_id]: 'tabs' }))
                          }}
                          onKeyDown={(e) => {
                            if (e.key === 'Enter' || e.key === ' ') {
                              e.preventDefault()
                              setActiveNode(active.swarm_id, id)
                              setViewModeBySwarm((prev) => ({ ...prev, [active.swarm_id]: 'tabs' }))
                            }
                          }}
                        >
                          {needsAttention && (
                            <span className="absolute z-20 top-1.5 right-1.5 w-2 h-2 rounded-full bg-amber-400 animate-pulse" />
                          )}
                          {!needsAttention && isReady && (
                            <span className="absolute z-20 top-1.5 right-1.5 w-2 h-2 rounded-full bg-emerald-400" />
                          )}
                          {isWorking && (
                            <span className="absolute z-20 bottom-1.5 right-1.5 w-2 h-2 rounded-full bg-rose-500 animate-pulse" />
                          )}

                          <div
                            style={{
                              width: baseWidth,
                              height: baseHeight,
                              transform: `scale(${gridLayout.scale})`,
                              transformOrigin: 'top left'
                            }}
                            className="p-2"
                          >
                            <div className="text-[11px] text-slate-300 border-b border-slate-800 pb-1 mb-2 w-full text-left">
                              Agent {id}
                              <span className="ml-2 text-[10px] text-slate-500">{formatUsd(nodeSessionCost)}</span>
                            </div>
                            <div className="h-[240px] overflow-y-auto pr-1">
                              {renderTurns(
                                node.turns,
                                active.pending_approvals?.[id],
                                active.job_id,
                                id,
                                active.known_exec_policies
                              )}
                            </div>
                          </div>
                        </div>
                      )
                    })}
                  </div>
                </div>
              )
            })()}

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
                      let targetAlias: string | undefined
                      let selector: 'all' | 'nodes' | 'idle' | undefined
                      let replyToSender = false
                      const nodeIdSet = new Set(Object.keys(swarm.nodes).map((id) => Number(id)))

                      const crossAllMatch = trimmed.match(/^\/swarm\[(.+?)\]\/all(\/reply)?\s+([\s\S]+)$/)
                      const crossIdleMatch = trimmed.match(/^\/swarm\[(.+?)\]\/(idle|first-idle)(\/reply)?\s+([\s\S]+)$/)
                      const crossAgentMatch = trimmed.match(/^\/swarm\[(.+?)\]\/(?:agent|node)\[(.+?)\](\/reply)?\s*([\s\S]+)$/)

                      if (crossAllMatch) {
                        targetAlias = crossAllMatch[1].trim()
                        replyToSender = Boolean(crossAllMatch[2])
                        promptText = crossAllMatch[3].trim()
                        if (!targetAlias || !promptText) return
                        selector = 'all'
                        targetNodes = 'all'
                      } else if (crossIdleMatch) {
                        targetAlias = crossIdleMatch[1].trim()
                        replyToSender = Boolean(crossIdleMatch[3])
                        promptText = crossIdleMatch[4].trim()
                        if (!targetAlias || !promptText) return
                        selector = 'idle'
                      } else if (crossAgentMatch) {
                        targetAlias = crossAgentMatch[1].trim()
                        const expr = crossAgentMatch[2].trim()
                        replyToSender = Boolean(crossAgentMatch[3])
                        promptText = crossAgentMatch[4].trim()
                        if (!targetAlias || !promptText) return
                        selector = 'nodes'
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
                        const resolvedNodes = Array.from(resolved)
                        if (resolvedNodes.length === 0) return
                        targetNodes = resolvedNodes
                      } else {
                        const allMatch = trimmed.match(/^\/all\s+([\s\S]+)$/)
                        if (allMatch) {
                          promptText = allMatch[1].trim()
                          if (!promptText) return
                          targetNodes = 'all'
                        } else {
                        const agentMatch = trimmed.match(/^\/(?:agent|node)\[(.+?)\]\s*([\s\S]+)$/)
                        if (agentMatch) {
                          const expr = agentMatch[1].trim()
                          promptText = agentMatch[2].trim()
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
                      }

                      setPendingPrompt(promptText)
                      // Provisional bubbles are only added for local swarm routing.
                      if (!targetAlias) {
                        const updatedNodes = { ...swarm.nodes }
                        const nodeIds =
                          targetNodes === 'all'
                            ? Object.keys(swarm.nodes).map((id) => Number(id))
                            : targetNodes

                        nodeIds.forEach((nodeId) => {
                          const provisional: NodeTurn = {
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
                      }

                      const apiBase = `${window.location.protocol}//${window.location.hostname}:4000`
                      const localNodeIds =
                        targetNodes === 'all'
                          ? Object.keys(swarm.nodes).map((id) => Number(id))
                          : targetNodes
                      const payload = targetAlias
                        ? {
                            prompt: promptText,
                            target_alias: targetAlias,
                            reply_to_sender: replyToSender,
                            source_node_id: activeNodeId,
                            selector: selector ?? (targetNodes === 'all' ? 'all' : 'nodes'),
                            ...(targetNodes === 'all' ? {} : { nodes: targetNodes })
                          }
                        : (
                          targetNodes === 'all'
                            ? { prompt: promptText }
                            : { prompt: promptText, nodes: localNodeIds }
                        )
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
              <div className="text-xs text-slate-500 mt-1">
                Enter to send. Supported prefixes: <code>/all</code>, <code>/agent[0,2-4]</code>, <code>/swarm[alias]/idle</code>, <code>/swarm[alias]/idle/reply</code>, <code>/swarm[alias]/all</code>, <code>/swarm[alias]/agent[...]</code>.
              </div>
            </div>
          </div>
        )}
      </div>

      {showLaunch && <LaunchModal onClose={() => setShowLaunch(false)} />}
    </div>
  )
}
