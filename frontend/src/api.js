const BASE = import.meta.env.VITE_API_URL || 'http://127.0.0.1:8765/api'

const req = async (path, opts) => {
  const r = await fetch(BASE + path, opts)
  if (!r.ok) throw Object.assign(new Error(`${r.status} ${path}`), { status: r.status })
  return r
}
const json = (path, opts) => req(path, opts).then((r) => r.json())
const post = (path, body) =>
  req(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })

export async function login(email, password) {
  try {
    await post('/login', { email, password })
    return true
  } catch (e) {
    if (e.status === 401) return false
    throw e
  }
}
export const listConversations = () => json('/conversations')
export const createConversation = () => post('/conversations', {}).then((r) => r.json())
export const getConversation = (id) => json(`/conversations/${id}`)
export const sendMessage = (id, text) => post(`/conversations/${id}/messages`, { text })
export const deleteConversation = (id) => req(`/conversations/${id}`, { method: 'DELETE' })

// onReconnect runs when the stream reopens after an error: events sent meanwhile were missed.
export function subscribe(id, onEvent, onReconnect) {
  const es = new EventSource(`${BASE}/conversations/${id}/events`)
  let lost = false
  es.onmessage = (e) => onEvent(JSON.parse(e.data))
  es.onerror = () => { lost = true }
  es.onopen = () => { if (lost) { lost = false; onReconnect?.() } }
  return () => es.close()
}
