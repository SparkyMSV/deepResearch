const BASE = import.meta.env.VITE_API_URL || 'http://127.0.0.1:8765/api'

const json = async (path, opts) => {
  const r = await fetch(BASE + path, opts)
  if (!r.ok) throw new Error(`${r.status} ${path}`)
  return r.json()
}
const post = (path, body) =>
  fetch(BASE + path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })

export async function login(email, password) {
  const r = await post('/login', { email, password })
  return r.ok
}
export const listConversations = () => json('/conversations')
export const createConversation = () => post('/conversations', {}).then((r) => r.json())
export const getConversation = (id) => json(`/conversations/${id}`)
export const sendMessage = (id, text) => post(`/conversations/${id}/messages`, { text })

export function subscribe(id, onEvent) {
  const es = new EventSource(`${BASE}/conversations/${id}/events`)
  es.onmessage = (e) => onEvent(JSON.parse(e.data))
  return () => es.close()
}
