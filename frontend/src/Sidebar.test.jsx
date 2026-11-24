import { render, screen, fireEvent, cleanup, act } from '@testing-library/react'
import { describe, it, expect, vi, afterEach } from 'vitest'

vi.mock('./api.js', () => ({
  listConversations: vi.fn(() => Promise.resolve([{ id: 1, title: 'Bond research' }, { id: 2, title: 'Other' }])),
  deleteConversation: vi.fn(() => Promise.resolve()),
}))
import * as api from './api.js'
import Sidebar from './Sidebar.jsx'

afterEach(cleanup)

describe('Sidebar', () => {
  it('deletes a research only after confirming', async () => {
    const onDeleted = vi.fn()
    render(<Sidebar activeId={1} onDeleted={onDeleted} theme="dark" refreshKey={0} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Delete Bond research' }))
    expect(screen.getByText('Delete this research?')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(api.deleteConversation).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Delete Bond research' }))
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Delete' })))
    expect(api.deleteConversation).toHaveBeenCalledWith(1)
    expect(onDeleted).toHaveBeenCalledWith(1)
    expect(screen.queryByText('Bond research')).toBeNull()
    expect(screen.getByText('Other')).toBeTruthy()
  })
})
