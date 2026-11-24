import { render, screen, fireEvent, cleanup, act, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

let emit, reconnect
vi.mock('./api.js', () => ({
  getConversation: vi.fn(),
  createConversation: vi.fn(),
  sendMessage: vi.fn(() => new Promise(() => {})),
  subscribe: vi.fn((id, cb, onReconnect) => { emit = cb; reconnect = onReconnect; return () => {} }),
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

  it('task event keeps existing subtasks', async () => {
    render(<Chat conversationId={1} />)
    await screen.findByText('Find prices')
    act(() => emit({ type: 'task', task: { id: 7, status: 'done', objective: 'Solar market' } }))
    expect(screen.getByText('Find prices')).toBeTruthy()
  })

  it('subtask event before its task renders a stub', async () => {
    render(<Chat conversationId={1} />)
    await screen.findByText('Find prices')
    act(() => emit({ type: 'subtask', subtask: { id: 90, task_id: 9, objective: 'Orphan step', status: 'running', round: 1, loop: 1, restarts: 0 } }))
    expect(screen.getByText('Orphan step')).toBeTruthy()
  })

  it('duplicate round event is not duplicated', async () => {
    render(<Chat conversationId={1} />)
    fireEvent.click(await screen.findByText('Find prices'))
    const round = { subtask_id: 70, loop: 1, n: 1, query: 'solar cost', hits: 4 }
    act(() => { emit({ type: 'round', round }); emit({ type: 'round', round }) })
    expect(screen.getAllByText(/solar cost/).length).toBe(1)
  })

  it('javascript: url is not a link', async () => {
    render(<Chat conversationId={1} />)
    await screen.findByText('Find prices')
    const content = JSON.stringify([{ title: 'Bad', url: 'javascript:alert(1)', snippet: 's' }, { title: 'Good', url: 'https://a.com/x', snippet: 's' }])
    act(() => emit({ type: 'message', message: { id: 9, role: 'assistant', content, kind: 'search_results', task_id: null } }))
    expect(screen.queryByRole('link', { name: 'Bad' })).toBeNull()
    expect(screen.getByRole('link', { name: 'Good' })).toBeTruthy()
  })

  it('failed send keeps the text and shows an error', async () => {
    api.sendMessage.mockRejectedValueOnce(new Error('500'))
    render(<Chat conversationId={1} />)
    await screen.findByText('Find prices')
    const box = screen.getByRole('textbox', { name: 'Message' })
    fireEvent.change(box, { target: { value: 'keep me' } })
    fireEvent.keyDown(box, { key: 'Enter' })
    expect(await screen.findByText(/not sent/)).toBeTruthy()
    expect(box.value).toBe('keep me')
  })

  it('refetches the conversation after the stream reconnects', async () => {
    render(<Chat conversationId={1} />)
    await screen.findByText('Find prices')
    const n = api.getConversation.mock.calls.length
    await act(async () => reconnect())
    expect(api.getConversation.mock.calls.length).toBe(n + 1)
  })

  it('shows a busy verb on send, streams deltas, then clears on the final message', async () => {
    api.sendMessage.mockResolvedValueOnce({})
    render(<Chat conversationId={1} />)
    await screen.findByText('Find prices')
    const box = screen.getByRole('textbox', { name: 'Message' })
    fireEvent.change(box, { target: { value: 'hi' } })
    await act(async () => fireEvent.keyDown(box, { key: 'Enter' }))
    expect(screen.getByRole('status').textContent).toMatch(/Thinking|Pondering|Considering/)
    act(() => emit({ type: 'status', text: 'searching' }))
    expect(screen.getByRole('status').textContent).toMatch(/Searching|Exploring|Gathering|Sifting|Reading/)
    act(() => { emit({ type: 'delta', text: 'Hel' }); emit({ type: 'delta', text: 'lo' }) })
    expect(screen.getByText('Hello')).toBeTruthy()
    expect(screen.queryByRole('status')).toBeNull()
    act(() => { emit({ type: 'message', message: { id: 9, role: 'assistant', kind: 'text', content: 'Hello there', task_id: null } }); emit({ type: 'status', text: null }) })
    expect(screen.getByText('Hello there')).toBeTruthy()
    expect(screen.queryByText('Hello')).toBeNull()
    expect(screen.queryByRole('status')).toBeNull()
  })

  it('landing view: chat box first, conversation created on send, message sent once the stream is ready', async () => {
    api.createConversation.mockResolvedValueOnce({ id: 42 })
    api.sendMessage.mockResolvedValueOnce({})
    const onCreated = vi.fn()
    const { rerender } = render(<Chat conversationId={null} onCreated={onCreated} />)
    expect(screen.getByRole('heading').textContent).toMatch(/Good (morning|afternoon|evening)/)
    expect(screen.queryByText(/Select or start/)).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: /search: latest RBI/ }))
    const box = screen.getByRole('textbox', { name: 'Message' })
    expect(box.value).toMatch(/^search: latest RBI/)
    fireEvent.change(box, { target: { value: 'hello there' } })
    await act(async () => fireEvent.keyDown(box, { key: 'Enter' }))
    expect(onCreated).toHaveBeenCalledWith(42)
    expect(api.sendMessage).not.toHaveBeenCalledWith(42, 'hello there')
    rerender(<Chat conversationId={42} onCreated={onCreated} />)
    await act(async () => emit({ type: 'ready' }))
    expect(api.sendMessage).toHaveBeenCalledWith(42, 'hello there')
  })
})
