import { useEffect, useState } from 'react'
import { listConversations } from './api.js'
import ThemeToggle from './ThemeToggle.jsx'

export default function Sidebar({ activeId, onSelect, onNew, theme, onToggleTheme, refreshKey }) {
  const [items, setItems] = useState([])

  useEffect(() => {
    listConversations().then(setItems).catch(() => setItems([]))
  }, [refreshKey])

  return (
    <aside className="side">
      <div className="logo">deep<b>.</b>research</div>
      <button className="new" onClick={onNew}>+ New research</button>
      <div className="muted lbl">Recent</div>
      {items.map((c) => (
        <button key={c.id} className={'item' + (c.id === activeId ? ' on' : '')} onClick={() => onSelect(c.id)}>
          {c.title}
        </button>
      ))}
      <ThemeToggle theme={theme} onToggle={onToggleTheme} />
    </aside>
  )
}
