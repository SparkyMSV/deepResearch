import { useEffect, useState } from 'react'

const VERBS = {
  thinking: ['Thinking', 'Pondering', 'Considering'],
  searching: ['Searching', 'Exploring', 'Gathering', 'Sifting', 'Reading'],
  writing: ['Writing', 'Composing'],
}

// Rotating verb while Qwen is busy; unknown phases are shown as given.
export default function Busy({ phase }) {
  const verbs = VERBS[phase] || [phase]
  const [i, setI] = useState(0)
  useEffect(() => {
    setI(0)
    const t = setInterval(() => setI((n) => n + 1), 1600)
    return () => clearInterval(t)
  }, [phase])
  return <div className="busy" role="status" aria-live="polite">{verbs[i % verbs.length]}…</div>
}
