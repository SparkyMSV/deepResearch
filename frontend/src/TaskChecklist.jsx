import { useState } from 'react'

const DOT = { running: 'run', checking_in: 'run', done: 'done', queued: 'q', paused_quota: 'warn', partial: 'warn', interrupted: 'warn' }

function tag(s) {
  const n = s.round || 0
  switch (s.status) {
    case 'running': case 'checking_in': return `round ${n} · check-in at ${n === 0 ? 5 : Math.ceil(n / 5) * 5}`
    case 'done': return `done · ${n} rounds`
    case 'paused_quota': return 'paused — quota'
    case 'restarted': return 'restarted'
    default: return s.status
  }
}

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
          {rounds.map((r) => <li key={`${r.loop}-${r.n}`}>{r.n}. {r.query} <span className="muted">· {r.hits} hits</span></li>)}
        </ul>
      )}
    </div>
  )
}

export default function TaskChecklist({ task }) {
  return (
    <div className="box" role="group" aria-label={`Task: ${task.objective}`}>
      {task.subtasks.map((s) => <Row key={s.id} s={s} />)}
    </div>
  )
}
