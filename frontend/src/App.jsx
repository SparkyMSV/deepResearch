import { useEffect, useState } from 'react'
import Login from './Login.jsx'
import ThemeToggle from './ThemeToggle.jsx'
import Sidebar from './Sidebar.jsx'
import Chat from './Chat.jsx'

// localStorage/sessionStorage can throw (private mode); never let that break the UI.
const read = (store, key) => { try { return window[store].getItem(key) } catch { return null } }
const write = (store, key, v) => { try { window[store].setItem(key, v) } catch { /* ignore */ } }

function initialTheme() {
  const saved = read('localStorage', 'theme')
  if (saved === 'dark' || saved === 'light') return saved
  return window.matchMedia?.('(prefers-color-scheme: light)').matches ? 'light' : 'dark'
}

export default function App() {
  const [theme, setTheme] = useState(initialTheme)
  const [authed, setAuthed] = useState(() => read('sessionStorage', 'auth') === '1')
  const [activeId, setActiveId] = useState(null)
  const [refreshKey, setRefreshKey] = useState(0)

  useEffect(() => {
    document.documentElement.dataset.theme = theme
    write('localStorage', 'theme', theme)
  }, [theme])

  const toggleTheme = () => setTheme((t) => (t === 'dark' ? 'light' : 'dark'))

  if (!authed) {
    return (
      <>
        <div style={{ position: 'fixed', top: 12, right: 12 }}><ThemeToggle theme={theme} onToggle={toggleTheme} /></div>
        <Login onSuccess={() => { write('sessionStorage', 'auth', '1'); setAuthed(true) }} />
      </>
    )
  }

  // New research = back to the landing chat; the conversation is created on its first message.
  const newResearch = () => setActiveId(null)
  const onCreated = (id) => { setActiveId(id); setRefreshKey((k) => k + 1) }
  const onDeleted = (id) => { if (id === activeId) setActiveId(null); setRefreshKey((k) => k + 1) }

  // The backend titles a conversation on its first message; refetch the sidebar then and again shortly after.
  function onSent() {
    setRefreshKey((k) => k + 1)
    setTimeout(() => setRefreshKey((k) => k + 1), 1500)
  }

  return (
    <div className="shell">
      <Sidebar activeId={activeId} onSelect={setActiveId} onNew={newResearch} onDeleted={onDeleted} theme={theme} onToggleTheme={toggleTheme} refreshKey={refreshKey} />
      <main className="main">
        <Chat conversationId={activeId} onSent={onSent} onCreated={onCreated} />
      </main>
    </div>
  )
}
