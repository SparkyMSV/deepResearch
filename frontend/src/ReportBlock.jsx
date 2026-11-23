import { useState } from 'react'
import Markdown from 'react-markdown'

const slug = (s) => (s || 'report').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '') || 'report'

export default function ReportBlock({ content, objective }) {
  const [open, setOpen] = useState(false)
  const title = (content.match(/^#+\s+(.*)$/m) || [])[1] || objective || 'Report'
  const preview = content.replace(/^#+\s+.*$/m, '').trim().split('\n').slice(0, 3).join('\n')

  function download() {
    const url = URL.createObjectURL(new Blob([content], { type: 'text/markdown' }))
    const a = document.createElement('a')
    a.href = url
    a.download = slug(objective) + '.md'
    a.click()
    URL.revokeObjectURL(url)
  }

  return (
    <section className="report" aria-label="Report">
      <div className="report-title">{title}</div>
      <div className={'md' + (open ? '' : ' clamp')}>
        <Markdown>{open ? content : preview}</Markdown>
      </div>
      <div className="report-actions">
        <button className="ghost" onClick={() => setOpen(!open)} aria-expanded={open}>{open ? 'Collapse' : 'Expand'}</button>
        <button className="ghost" onClick={download}>Download .md</button>
      </div>
    </section>
  )
}
