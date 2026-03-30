import { useEffect, useRef, useState } from 'react'
import { useSwarmStore } from './store'
import { getBackendHttpOrigin, getBackendWsOrigin } from './runtime'

export function useWebSocket() {
  const handleMessage = useSwarmStore((s) => s.handleMessage)
  const setSwarms = useSwarmStore((s) => s.setSwarms)
  const setProjects = useSwarmStore((s) => s.setProjects)

  const [status, setStatus] = useState<'connecting' | 'connected' | 'reconnecting' | 'disconnected'>('connecting')
  const retryCount = useRef(0)
  const socketRef = useRef<WebSocket | null>(null)
  const reconnectTimer = useRef<NodeJS.Timeout | null>(null)
  const approvalsPollTimer = useRef<NodeJS.Timeout | null>(null)

  useEffect(() => {
    let isMounted = true

    const connect = () => {
      const wsUrl = getBackendWsOrigin()

      setStatus(retryCount.current === 0 ? 'connecting' : 'reconnecting')

      const ws = new WebSocket(wsUrl)
      socketRef.current = ws

      ws.onopen = () => {
        if (!isMounted) return
        console.log('WebSocket connected')
        retryCount.current = 0
        setStatus('connected')

        // Reconcile control plane after reconnect
        const apiBase = getBackendHttpOrigin()
        fetch(`${apiBase}/swarms`)
          .then((res) => res.json())
          .then((data) => setSwarms(data))
          .catch(() => {})
        fetch(`${apiBase}/approvals`)
          .then((res) => res.json())
          .then((data) => handleMessage({ type: 'approvals_snapshot', payload: data }))
          .catch(() => {})
        fetch(`${apiBase}/projects`)
          .then((res) => res.json())
          .then((data) => setProjects(data))
          .catch(() => {})

        if (approvalsPollTimer.current) clearInterval(approvalsPollTimer.current)
        approvalsPollTimer.current = setInterval(() => {
          fetch(`${apiBase}/approvals`)
            .then((res) => res.json())
            .then((data) => handleMessage({ type: 'approvals_snapshot', payload: data }))
            .catch(() => {})
        }, 1000)
      }

      ws.onmessage = (event) => {
        try {
          const msg = JSON.parse(event.data)
          if (msg?.type === 'workspace_archive_ready' && typeof msg?.payload?.download_url === 'string') {
            const apiBase = getBackendHttpOrigin()
            const href = `${apiBase}${msg.payload.download_url}`
            const a = document.createElement('a')
            a.href = href
            a.download = typeof msg?.payload?.archive_name === 'string' ? msg.payload.archive_name : ''
            document.body.appendChild(a)
            a.click()
            a.remove()
          }
          if (msg?.type === 'workspace_archive_failed' && typeof msg?.payload?.reason === 'string') {
            console.warn('Workspace archive export failed:', msg.payload.reason)
          }
          handleMessage(msg)
        } catch (err) {
          console.error('WS parse error', err)
        }
      }

      ws.onclose = () => {
        if (!isMounted) return
        console.log('WebSocket disconnected')
        setStatus('disconnected')
        if (approvalsPollTimer.current) {
          clearInterval(approvalsPollTimer.current)
          approvalsPollTimer.current = null
        }

        // Exponential backoff
        const delay = Math.min(1000 * 2 ** retryCount.current, 10000)
        retryCount.current += 1

        reconnectTimer.current = setTimeout(() => {
          connect()
        }, delay)
      }

      ws.onerror = () => {
        ws.close()
      }
    }

    connect()

    return () => {
      isMounted = false
      if (reconnectTimer.current) clearTimeout(reconnectTimer.current)
      if (approvalsPollTimer.current) clearInterval(approvalsPollTimer.current)
      if (socketRef.current) socketRef.current.close()
    }
  }, [handleMessage, setProjects, setSwarms])

  return { status }
}
