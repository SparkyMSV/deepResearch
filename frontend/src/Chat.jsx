import { useEffect, useRef, useState } from 'react'
import { getConversation, sendMessage, subscribe } from './api.js'
import Message from './Message.jsx'
import TaskChecklist from './TaskChecklist.jsx'
import ReportBlock from './ReportBlock.jsx'

const same = (a, b) => a.id === b.id
const upsert = (list, item, merge = (x, y) => ({ ...x, ...y })) =>
  list.some((x) => same(x, item)) ? list.map((x) => (same(x, item) ? merge(x, item) : x)) : [...list, item]
const addRounds = (a = [], b = []) => [...a.filter((x) => !b.some((y) => y.loop === x.loop && y.n === x.n)), ...b]
const mergeSub = (a, b) => ({ ...a, ...b, rounds: addRounds(a.rounds, b.rounds) })
// Merge only the fields given; sub-tasks are upserted, never replaced.
const mergeTask = (a, b) => ({ ...a, ...b, subtasks: (b.subtasks || []).reduce((l, s) => upsert(l, s, mergeSub), a.subtasks || []) })
const upsertTask = (tasks, t) => upsert(tasks, { subtasks: [], ...t }, mergeTask)

function applyEvent(st, ev) {
  if (ev.type === 'message') return { ...st, messages: upsert(st.messages, ev.message) }
  if (ev.type === 'task') return { ...st, tasks: upsertTask(st.tasks, ev.task) }
  if (ev.type === 'subtask') {
    const sub = { rounds: st.pending[ev.subtask.id] || [], ...ev.subtask }
    return { ...st, tasks: upsertTask(st.tasks, { id: ev.subtask.task_id, subtasks: [sub] }) }
  }
  if (ev.type === 'round') {
    const r = ev.round
    const owner = st.tasks.find((t) => t.subtasks.some((x) => x.id === r.subtask_id))
    // Round before its sub-task: hold it until the subtask event arrives.
    if (!owner) return { ...st, pending: { ...st.pending, [r.subtask_id]: addRounds(st.pending[r.subtask_id], [r]) } }
    return { ...st, tasks: upsertTask(st.tasks, { id: owner.id, subtasks: [{ id: r.subtask_id, rounds: [r] }] }) }
  }
  return st
}

const EMPTY = { messages: [], tasks: [], pending: {} }

export default function Chat({ conversationId, onSent }) {
  const [st, setSt] = useState(EMPTY)
  const [text, setText] = useState('')
  const [sending, setSending] = useState(false)
  const input = useRef(null)
  const end = useRef(null)

  useEffect(() => {
    if (!conversationId) return
    let live = true
    setSt(EMPTY)
    const unsub = subscribe(conversationId, (ev) => live && setSt((s) => applyEvent(s, ev)))
    getConversation(conversationId)
      .then((c) => live && setSt((s) => ({
        // fetched rows are the base; events that arrived during the fetch go on top
        ...s,
        messages: s.messages.reduce((l, m) => upsert(l, m), c.messages),
        tasks: s.tasks.reduce(upsertTask, c.tasks),
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
