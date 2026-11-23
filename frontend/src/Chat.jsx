import { useEffect, useRef, useState } from 'react'
import { getConversation, sendMessage, subscribe } from './api.js'
import Message from './Message.jsx'
import TaskChecklist from './TaskChecklist.jsx'
import ReportBlock from './ReportBlock.jsx'

const upsert = (list, item) =>
  list.some((x) => x.id === item.id) ? list.map((x) => (x.id === item.id ? { ...x, ...item } : x)) : [...list, item]

function applyEvent(st, ev) {
  if (ev.type === 'message') return { ...st, messages: upsert(st.messages, ev.message) }
  if (ev.type === 'task') return { ...st, tasks: upsert(st.tasks, { subtasks: [], ...ev.task }) }
  const tasks = st.tasks.map((t) => {
    if (ev.type === 'subtask' && t.id === ev.subtask.task_id) return { ...t, subtasks: upsert(t.subtasks, ev.subtask) }
    if (ev.type === 'round' && t.subtasks.some((s) => s.id === ev.round.subtask_id)) {
      const subtasks = t.subtasks.map((s) => {
        if (s.id !== ev.round.subtask_id) return s
        const rounds = (s.rounds || []).filter((r) => !(r.loop === ev.round.loop && r.n === ev.round.n))
        return { ...s, rounds: [...rounds, ev.round] }
      })
      return { ...t, subtasks }
    }
    return t
  })
  return { ...st, tasks }
}

export default function Chat({ conversationId, onSent }) {
  const [st, setSt] = useState({ messages: [], tasks: [] })
  const [text, setText] = useState('')
  const [sending, setSending] = useState(false)
  const input = useRef(null)
  const end = useRef(null)

  useEffect(() => {
    if (!conversationId) return
    let live = true
    setSt({ messages: [], tasks: [] })
    const unsub = subscribe(conversationId, (ev) => live && setSt((s) => applyEvent(s, ev)))
    getConversation(conversationId)
      .then((c) => live && setSt((s) => ({
        // events that arrived during the fetch are kept; fetched rows fill in the rest
        messages: c.messages.reduce(upsert, s.messages),
        tasks: c.tasks.reduce((acc, t) => acc.some((x) => x.id === t.id) ? acc : [...acc, t], s.tasks),
      })))
      .catch((e) => console.error('getConversation failed', e))
    input.current?.focus()
    return () => { live = false; unsub() }
  }, [conversationId])

  useEffect(() => { end.current?.scrollIntoView?.({ block: 'end' }) }, [st.messages.length])

  if (!conversationId) return <div className="empty muted">Select or start a research</div>

  async function send() {
    const t = text.trim()
    if (!t || sending) return
    setSending(true)
    try {
      await sendMessage(conversationId, t)
      setText('')
      onSent?.()
    } catch (e) {
      console.error('sendMessage failed', e)
    } finally {
      setSending(false)
      input.current?.focus()
    }
  }

  // Checklist goes under the first message of its task (the "Planning…" line), else at the end.
  const first = {}
  st.messages.forEach((m) => { if (m.task_id && !(m.task_id in first)) first[m.task_id] = m.id })
  const placed = new Set(Object.keys(first).map(Number))
  const check = (t) => <TaskChecklist key={`t${t.id}`} task={t} />
  const objective = (id) => st.tasks.find((t) => t.id === id)?.objective

  return (
    <>
      <div className="chat">
        {st.messages.map((m) => (
          <div key={m.id} className="row-m">
            {m.kind === 'report'
              ? <ReportBlock content={m.content} objective={objective(m.task_id)} />
              : <Message m={m} />}
            {st.tasks.filter((t) => first[t.id] === m.id).map(check)}
          </div>
        ))}
        {st.tasks.filter((t) => !placed.has(t.id)).map(check)}
        <div ref={end} />
      </div>
      <div className="bar">
        <textarea
          ref={input} className="inp" rows={1} aria-label="Message" value={text} disabled={sending}
          placeholder="Ask anything — prefix with search: or task: to force a mode"
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() } }}
        />
        <button className="send" onClick={send} disabled={sending || !text.trim()} aria-label="Send">↑</button>
      </div>
    </>
  )
}
