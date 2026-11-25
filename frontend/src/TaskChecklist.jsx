import { useState } from 'react'

const DOT = { running: 'run', checking_in: 'run', restarted: 'run', failed: 'q', done: 'done', queued: 'q', paused_quota: 'warn', paused_memory: 'warn', partial: 'warn', interrupted: 'warn' }

const NOTE = {
  paused_quota: 'Paused — Gemini and Groq unavailable, retrying',
  queued: 'Queued — waiting for the current research to finish',
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
    case 'paused_memory': return 'paused — Mac memory low or MTPLX not running, retrying'
    default: return s.status
  }
}
const tag = (s) => status(s) + (s.restarts > 0 ? ` · restart ${s.restarts}` : '')
const closed = (f) => !!f.accepted
const DOC = { not_found: '✗ not found', planned: '… searching' }
const host = (u) => { try { return new URL(u).host } catch { return u || '' } }
const safe = (u) => (/^https?:\/\//.test(u || '') ? u : undefined)
const source = (f) => (f.source_key
  ? `${f.source_key}${f.page ? ` p.${f.page}` : ''}${f.printed_page && f.printed_page !== f.page ? ` (printed ${f.printed_page})` : ''}`
  : host(f.source_url))
function fieldState(f) {
  if (f.status === 'unavailable') return f.accepted ? `not public: ${f.note || ''}`.trim() : 'not public? (unreviewed)'
  if (f.accepted === 2) return '✓ confirmed'
  if (f.accepted) return '✓ checked locally'
  return f.status === 'found' ? 'unreviewed' : 'searching'
}

function Documents({ docs }) {
  return (
    <div className="t-docs">
      <div className="muted lbl">Documents</div>
      {docs.map((d) => (
        <div key={d.id} className="t-doc">
          {d.status === 'verified'
            ? <a href={safe(d.url)} target="_blank" rel="noreferrer">✓ {d.key} {d.about || d.name}</a>
            : <span>{d.key} {d.name} <span className="muted">{DOC[d.status] || d.status}</span></span>}
        </div>
      ))}
    </div>
  )
}

function Row({ s, fields }) {
  const [open, setOpen] = useState(false)
  const rounds = s.rounds || []
  const progress = fields.length ? `${fields.filter(closed).length}/${fields.length} fields · ` : ''
  return (
    <div className="t">
      <button className="t-head" onClick={() => setOpen(!open)} aria-expanded={open}>
        <span className="t-obj"><span className={'dot ' + (DOT[s.status] || 'q')} />{s.objective}</span>
        <span className={'tag' + (DOT[s.status] === 'run' ? ' on' : '')}>{progress + tag(s)}</span>
      </button>
      {open && fields.length > 0 && (
        <table className="t-fields"><tbody>
          {fields.map((f) => (
            <tr key={f.id}>
              <td>{f.name}</td><td className="muted">{f.period}</td>
              <td>{f.value ? `${f.value}${f.unit ? ` ${f.unit}` : ''}` : '—'}</td>
              <td className="muted">{f.value ? source(f) : ''}</td><td>{fieldState(f)}</td>
            </tr>
          ))}
        </tbody></table>
      )}
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
      {task.documents?.length > 0 && <Documents docs={task.documents} />}
      {task.subtasks.map((s) => <Row key={s.id} s={s} fields={(task.fields || []).filter((f) => f.subtask_id === s.id)} />)}
    </div>
  )
}
