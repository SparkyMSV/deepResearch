import { render, screen, fireEvent, cleanup, act, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

let emit
vi.mock('./api.js', () => ({
  getConversation: vi.fn(),
  sendMessage: vi.fn(() => new Promise(() => {})),
  subscribe: vi.fn((id, cb) => { emit = cb; return () => {} }),
}))
import * as api from './api.js'
import Chat from './Chat.jsx'

const conv = {
  id: 1, title: 't',
  messages: [
    { id: 1, role: 'user', content: 'research x', kind: 'text', task_id: null },
    { id: 2, role: 'assistant', content: 'Planning…', kind: 'progress', task_id: 7 },
    { id: 3, role: 'assistant', content: '# Report\n\nbody', kind: 'report', task_id: 7 },
  ],
  tasks: [{ id: 7, objective: 'Solar market', status: 'running', report: null,
    subtasks: [{ id: 70, task_id: 7, objective: 'Find prices', status: 'queued', round: 0, loop: 1, restarts: 0, rounds: [] }] }],
}

beforeEach(() => { api.getConversation.mockResolvedValue(conv); window.HTMLElement.prototype.scrollIntoView = vi.fn() })
afterEach(cleanup)

describe('Chat', () => {
  it('renders subtask status from event', async () => {
    render(<Chat conversationId={1} />)
    await screen.findByText('Find prices')
    act(() => emit({ type: 'subtask', subtask: { id: 70, task_id: 7, objective: 'Find prices', status: 'running', round: 3, loop: 1, restarts: 0 } }))
    expect(screen.getByText(/round 3 · check-in at 5/)).toBeTruthy()
  })

  it('report download builds markdown blob', async () => {
    URL.createObjectURL = vi.fn(() => 'blob:x'); URL.revokeObjectURL = vi.fn()
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    render(<Chat conversationId={1} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Download .md' }))
    const blob = URL.createObjectURL.mock.calls[0][0]
    expect(blob).toBeInstanceOf(Blob)
    expect(blob.type).toBe('text/markdown')
  })

  it('input stays enabled while task runs', async () => {
    render(<Chat conversationId={1} />)
    await screen.findByText('Find prices')
    const box = screen.getByRole('textbox', { name: 'Message' })
    expect(box.disabled).toBe(false)
    fireEvent.change(box, { target: { value: 'hi' } })
    fireEvent.keyDown(box, { key: 'Enter' })
    await waitFor(() => expect(api.sendMessage).toHaveBeenCalledWith(1, 'hi'))
    expect(box.disabled).toBe(true) // POST in flight only
  })
})
