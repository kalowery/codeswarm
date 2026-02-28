import { useEffect, useRef, useState } from 'react'
import { useSwarmStore } from './store'

export function useWebSocket() {
  const handleMessage = useSwarmStore((s) => s.handleMessage)
  const setSwarms = useSwarmStore((s) => s.setSwarms)

  const [status, setStatus] = useState<'connecting' | 'connected' | 'reconnecting' | 'disconnected'>('connecting')
  const retryCount = useRef(0)
  const socketRef = useRef<WebSocket | null>(null)
  const reconnectTimer = useRef<NodeJS.Timeout | null>(null)

  useEffect(() => {
    let isMounted = true

    const connect = () => {
      const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
      const wsUrl = `${wsProtocol}//${window.location.hostname}:4000`

      setStatus(retryCount.current === 0 ? 'connecting' : 'reconnecting')

      const ws = new WebSocket(wsUrl)
      socketRef.current = ws

      ws.onopen = () => {
        if (!isMounted) return
        console.log('WebSocket connected')
        retryCount.current = 0
        setStatus('connected')

        // Reconcile control plane after reconnect
        const apiBase = `${window.location.protocol}//${window.location.hostname}:4000`
        fetch(`${apiBase}/swarms`)
          .then((res) => res.json())
          .then((data) => setSwarms(data))
          .catch(() => {})
      }

      ws.onmessage = (event) => {
        try {
          const msg = JSON.parse(event.data)
          handleMessage(msg)
        } catch (err) {
          console.error('WS parse error', err)
        }
      }

      ws.onclose = () => {
        if (!isMounted) return
        console.log('WebSocket disconnected')
        setStatus('disconnected')

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
      if (socketRef.current) socketRef.current.close()
    }
  }, [handleMessage, setSwarms])

  return { status }
}
