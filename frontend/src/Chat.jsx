import { useEffect, useRef, useState } from 'react'
import { createConversation, getConversation, sendMessage, subscribe } from './api.js'
import Message from './Message.jsx'
import TaskChecklist from './TaskChecklist.jsx'
import ReportBlock from './ReportBlock.jsx'
import Busy from './Busy.jsx'

const same = (a, b) => a.id === b.id
const upsert = (list, item, merge = (x, y) => ({ ...x, ...y })) =>
  list.some((x) => same(x, item)) ? list.map((x) => (same(x, item) ? merge(x, item) : x)) : [...list, item]
const addRounds = (a = [], b = []) => [...a.filter((x) => !b.some((y) => y.loop === x.loop && y.n === x.n)), ...b]
const mergeSub = (a, b) => ({ ...a, ...b, rounds: addRounds(a.rounds, b.rounds) })
// Merge only the fields given; sub-tasks are upserted, never replaced.
const mergeTask = (a, b) => ({ ...a, ...b, subtasks: (b.subtasks || []).reduce((l, s) => upsert(l, s, mergeSub), a.subtasks || []) })
const upsertTask = (tasks, t) => upsert(tasks, { subtasks: [], ...t }, mergeTask)

export function applyEvent(st, ev) {
  if (ev.type === 'message') {
    // the final assistant message replaces the streamed draft
    const draft = ev.message.role === 'assistant' ? '' : st.draft
    return { ...st, draft, messages: upsert(st.messages, ev.message) }
  }
  if (ev.type === 'status') return { ...st, status: ev.text, draft: ev.text ? st.draft : '' }
  if (ev.type === 'delta') return { ...st, draft: st.draft + ev.text }
  if (ev.type === 'task') return { ...st, tasks: upsertTask(st.tasks, ev.task) }
  if (ev.type === 'subtask') {
    const sub = { rounds: st.pending[ev.subtask.id] || [], ...ev.subtask }
    return { ...st, tasks: upsertTask(st.tasks, { id: ev.subtask.task_id, subtasks: [sub] }) }
  }
  if (ev.type === 'document' || ev.type === 'field') {
    const key = ev.type === 'document' ? 'documents' : 'fields'
    const item = ev[ev.type]
    const t = st.tasks.find((x) => x.id === item.task_id)
    return { ...st, tasks: upsertTask(st.tasks, { id: item.task_id, [key]: upsert((t && t[key]) || [], item) }) }
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

const EMPTY = { messages: [], tasks: [], pending: {}, status: null, draft: '' }

const SUGGESTIONS = [
  'search: latest RBI repo rate decision',
  "task: How investable are Tata Steel's bonds? Build a credit view",
  'What can you help me research?',
]

function greeting() {
  const h = new Date().getHours()
  return h < 12 ? 'Good morning' : h < 17 ? 'Good afternoon' : 'Good evening'
}

export default function Chat({ conversationId, onSent, onCreated }) {
  const [st, setSt] = useState(EMPTY)
  const [text, setText] = useState('')
  const [sending, setSending] = useState(false)
  const [err, setErr] = useState('')
  const input = useRef(null)
  const end = useRef(null)
  const firstMessage = useRef(null)  // typed on the landing view; sent once the new conversation's stream is ready

  useEffect(() => {
    if (!conversationId) return
    let live = true
    setSt(EMPTY)
    setErr('')
    const load = () => getConversation(conversationId)
      .then((c) => live && setSt((s) => ({
        // fetched rows are the base; events that arrived during the fetch go on top
        ...s,
        messages: s.messages.reduce((l, m) => upsert(l, m), c.messages),
        tasks: s.tasks.reduce(upsertTask, c.tasks),
      })))
      .catch((e) => console.error('getConversation failed', e))
    const onEvent = (ev) => {
      if (!live) return
      if (ev.type === 'ready' && firstMessage.current) {
        const t = firstMessage.current
        firstMessage.current = null
        setSt((s) => ({ ...s, status: 'thinking' }))
        sendMessage(conversationId, t).then(() => onSent?.()).catch((e) => {
          console.error('sendMessage failed', e)
          setText(t)
          setSt((s) => ({ ...s, status: null }))
          setErr('Message not sent — check the connection and try again.')
        })
      }
      setSt((s) => applyEvent(s, ev))
    }
    const unsub = subscribe(conversationId, onEvent, load)
    load()
    input.current?.focus()
    return () => { live = false; unsub() }
  }, [conversationId])

  useEffect(() => { end.current?.scrollIntoView?.({ block: 'end' }) }, [st.messages.length, st.draft, st.status])

  async function send() {
    const t = text.trim()
    if (!t || sending) return
    setSending(true)
    setErr('')
    if (!conversationId) {  // landing view: create the conversation, send when its stream is live
      try {
        const { id } = await createConversation()
        firstMessage.current = t
        setText('')
        onCreated?.(id)
      } catch (e) {
        console.error('createConversation failed', e)
        setErr('Could not start a conversation — is the backend running?')
      } finally {
        setSending(false)
      }
      return
    }
    try {
      await sendMessage(conversationId, t)
      setText('')
      // show life immediately; the server's status events take over from here
      setSt((s) => ({ ...s, status: s.status || 'thinking' }))
      onSent?.()
    } catch (e) {
      console.error('sendMessage failed', e)
      setErr('Message not sent — check the connection and try again.')
    } finally {
      setSending(false)
      input.current?.focus()
    }
  }

  const composer = (
    <>
      {err && <div className="send-err muted" role="alert">{err}</div>}
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

  if (!conversationId) {
    return (
      <div className="home">
        <h1 className="greet">{greeting()}</h1>
        <p className="muted">Ask anything. Start with <code>search:</code> for a quick lookup or <code>task:</code> for a deep, cited report.</p>
        <div className="home-box">{composer}</div>
        <div className="chips">
          {SUGGESTIONS.map((q) => (
            <button key={q} className="chip" onClick={() => { setText(q); input.current?.focus() }}>{q}</button>
          ))}
        </div>
      </div>
    )
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
        {st.draft
          ? <div className="msg-a md streaming">{st.draft}</div>
          : st.status && <Busy phase={st.status} />}
        <div ref={end} />
      </div>
      {composer}
    </>
  )
}
