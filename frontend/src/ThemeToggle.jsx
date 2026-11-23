export default function ThemeToggle({ theme, onToggle }) {
  return (
    <button className="toggle" aria-label="Toggle theme" onClick={onToggle}>
      {theme === 'dark' ? 'Light mode' : 'Dark mode'}
    </button>
  )
}
