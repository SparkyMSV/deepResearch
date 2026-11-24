import { useEffect, useState } from 'react'
import { deleteConversation, listConversations } from './api.js'
import ThemeToggle from './ThemeToggle.jsx'

export default function Sidebar({ activeId, onSelect, onNew, onDeleted, theme, onToggleTheme, refreshKey }) {
  const [items, setItems] = useState([])
  const [confirming, setConfirming] = useState(null)  // id awaiting "Delete?" confirmation

  useEffect(() => {
    listConversations().then(setItems).catch(() => setItems([]))
  }, [refreshKey])

  async function remove(id) {
    setConfirming(null)
    try {
      await deleteConversation(id)
      setItems((l) => l.filter((c) => c.id !== id))
      onDeleted?.(id)
    } catch (e) {
      console.error('deleteConversation failed', e)
    }
  }

  return (
    <aside className="side">
      <div className="logo">deep<b>.</b>research</div>
      <button className="new" onClick={onNew}>+ New research</button>
      <div className="muted lbl">Recent</div>
      {items.map((c) => (
        confirming === c.id ? (
          <div key={c.id} className="item confirm">
            <span>Delete this research?</span>
            <button className="link danger" onClick={() => remove(c.id)}>Delete</button>
            <button className="link" onClick={() => setConfirming(null)}>Cancel</button>
          </div>
        ) : (
          <div key={c.id} className={'item-row' + (c.id === activeId ? ' on' : '')}>
            <button className="item" onClick={() => onSelect(c.id)}>{c.title}</button>
            <button className="del" aria-label={`Delete ${c.title}`} title="Delete" onClick={() => setConfirming(c.id)}>×</button>
          </div>
        )
      ))}
      <ThemeToggle theme={theme} onToggle={onToggleTheme} />
    </aside>
  )
}
