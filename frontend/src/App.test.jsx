import { render, screen, fireEvent, cleanup } from '@testing-library/react'
import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import App from './App.jsx'

beforeEach(() => {
  sessionStorage.clear()
  localStorage.clear()
  delete document.documentElement.dataset.theme
})
afterEach(cleanup)

describe('App shell', () => {
  it('shows login when signed out', () => {
    render(<App />)
    expect(screen.getByRole('button', { name: 'Sign in' })).toBeTruthy()
    expect(screen.getByText('demo: analyst@demo / password')).toBeTruthy()
  })

  it('theme toggle flips data-theme', () => {
    render(<App />)
    expect(document.documentElement.dataset.theme).toBe('dark')
    fireEvent.click(screen.getByRole('button', { name: /theme/i }))
    expect(document.documentElement.dataset.theme).toBe('light')
  })
})
