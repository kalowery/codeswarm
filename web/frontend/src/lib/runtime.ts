const backendOriginFromEnv = process.env.NEXT_PUBLIC_CODESWARM_BACKEND_ORIGIN?.trim()
const backendPortFromEnv = process.env.NEXT_PUBLIC_CODESWARM_BACKEND_PORT?.trim() || '4000'

export function getBackendHttpOrigin() {
  if (backendOriginFromEnv) return backendOriginFromEnv
  if (typeof window === 'undefined') return `http://127.0.0.1:${backendPortFromEnv}`
  return `${window.location.protocol}//${window.location.hostname}:${backendPortFromEnv}`
}

export function getBackendWsOrigin() {
  if (backendOriginFromEnv) {
    if (backendOriginFromEnv.startsWith('https://')) {
      return `wss://${backendOriginFromEnv.slice('https://'.length)}`
    }
    if (backendOriginFromEnv.startsWith('http://')) {
      return `ws://${backendOriginFromEnv.slice('http://'.length)}`
    }
    return backendOriginFromEnv
  }
  if (typeof window === 'undefined') return `ws://127.0.0.1:${backendPortFromEnv}`
  const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${wsProtocol}//${window.location.hostname}:${backendPortFromEnv}`
}
