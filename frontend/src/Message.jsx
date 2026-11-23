import Markdown from 'react-markdown'

const domain = (u) => { try { return new URL(u).hostname.replace(/^www\./, '') } catch { return u } }

export default function Message({ m }) {
  if (m.kind === 'progress') return <div className="msg-p muted">{m.content}</div>
  if (m.kind === 'search_results') {
    let hits = []
    try { hits = JSON.parse(m.content) } catch { /* show empty list */ }
    return (
      <ol className="msg-a hits">
        {hits.map((h, i) => (
          <li key={i}>
            <a href={h.url} target="_blank" rel="noreferrer">{h.title}</a>
            <span className="muted"> {domain(h.url)}</span>
            <div className="muted">{h.snippet}</div>
          </li>
        ))}
      </ol>
    )
  }
  if (m.role === 'user') return <div className="msg-u">{m.content}</div>
  return <div className="msg-a md"><Markdown>{m.content}</Markdown></div>
}
