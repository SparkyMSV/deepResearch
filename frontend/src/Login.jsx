import { useState } from 'react'
import { login } from './api.js'

export default function Login({ onSuccess }) {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')

  async function submit(e) {
    e.preventDefault()
    try {
      if (await login(email, password)) return onSuccess()
      setError('Invalid email or password')
    } catch {
      setError('Cannot reach the server')
    }
  }

  return (
    <div className="login">
      <form className="card" onSubmit={submit}>
        <div className="logo">deep<b>.</b>research</div>
        <div className="muted" style={{ fontSize: 12 }}>Sign in to continue</div>
        <input className="fld" type="email" placeholder="Email" aria-label="Email" value={email} onChange={(e) => setEmail(e.target.value)} />
        <input className="fld" type="password" placeholder="Password" aria-label="Password" value={password} onChange={(e) => setPassword(e.target.value)} />
        {error && <div className="err" role="alert">{error}</div>}
        <button className="btn" type="submit">Sign in</button>
        <div className="hint muted">demo: analyst@demo / password</div>
      </form>
    </div>
  )
}
