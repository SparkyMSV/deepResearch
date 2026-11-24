import { useState } from 'react'

const DOT = { running: 'run', checking_in: 'run', restarted: 'run', failed: 'q', done: 'done', queued: 'q', paused_quota: 'warn', partial: 'warn', interrupted: 'warn' }

const NOTE = {
  paused_quota: 'Paused — Gemini and Groq unavailable, retrying',
  interrupted: 'Interrupted — server restarted. Ask again to rerun.',
  failed: 'Stopped',
}

function status(s) {
  const n = s.round || 0
  switch (s.status) {
    case 'running': return `round ${n} · check-in at ${n === 0 ? 5 : Math.ceil(n / 5) * 5}`
    case 'checking_in': return 'checking in'
    case 'done': return `done · ${n} rounds`
    case 'paused_quota': return 'paused — models unavailable, retrying'
    default: return s.status
  }
}
const tag = (s) => status(s) + (s.restarts > 0 ? ` · restart ${s.restarts}` : '')

function Row({ s }) {
  const [open, setOpen] = useState(false)
  const rounds = s.rounds || []
  return (
    <div className="t">
      <button className="t-head" onClick={() => setOpen(!open)} aria-expanded={open}>
        <span className="t-obj"><span className={'dot ' + (DOT[s.status] || 'q')} />{s.objective}</span>
        <span className={'tag' + (DOT[s.status] === 'run' ? ' on' : '')}>{tag(s)}</span>
      </button>
      {open && (
        <ul className="t-log">
          {rounds.length === 0 && <li className="muted">No rounds yet</li>}
          {rounds.map((r) => <li key={`${r.loop}-${r.n}`}>{r.loop > 0 ? `L${r.loop}·${r.n}` : r.n}. {r.query} <span className="muted">· {r.hits} hits</span></li>)}
        </ul>
      )}
    </div>
  )
}

export default function TaskChecklist({ task }) {
  return (
    <div className="box" role="group" aria-label={`Task: ${task.objective}`}>
      {NOTE[task.status] && <div className="t-note muted">{NOTE[task.status]}</div>}
      {task.subtasks.map((s) => <Row key={s.id} s={s} />)}
    </div>
  )
}
