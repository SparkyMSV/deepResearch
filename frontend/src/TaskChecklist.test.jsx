import { render, screen, fireEvent, cleanup } from '@testing-library/react'
import { describe, it, expect, afterEach } from 'vitest'
import TaskChecklist from './TaskChecklist.jsx'

afterEach(cleanup)
const task = (status, subtasks = []) => ({ id: 1, objective: 'x', status, subtasks })
const sub = (o) => ({ id: 2, task_id: 1, objective: 'Find prices', status: 'running', round: 0, loop: 0, restarts: 0, rounds: [], ...o })

describe('TaskChecklist', () => {
  it.each([
    ['paused_quota', 'Paused — Gemini and Groq unavailable, retrying'],
    ['interrupted', 'Interrupted — server restarted. Ask again to rerun.'],
    ['failed', 'Stopped'],
  ])('shows task-level %s with no sub-tasks', (status, text) => {
    render(<TaskChecklist task={task(status)} />)
    expect(screen.getByText(text)).toBeTruthy()
  })

  it('no header line while running', () => {
    render(<TaskChecklist task={task('running')} />)
    expect(screen.queryByText(/Paused|Interrupted|Stopped/)).toBeNull()
  })

  it('sub-task tags: paused, checking in, restarts', () => {
    render(<TaskChecklist task={task('running', [
      sub({ id: 2, status: 'paused_quota' }),
      sub({ id: 3, status: 'checking_in', restarts: 1 }),
    ])} />)
    expect(screen.getByText('paused — models unavailable, retrying')).toBeTruthy()
    expect(screen.getByText('checking in · restart 1')).toBeTruthy()
  })

  it('round log prefixes loop when loop > 0', () => {
    render(<TaskChecklist task={task('running', [sub({ rounds: [
      { loop: 0, n: 2, query: 'first', hits: 1 }, { loop: 1, n: 3, query: 'second', hits: 1 },
    ] })])} />)
    fireEvent.click(screen.getByText('Find prices'))
    expect(screen.getByText(/^2\. first/)).toBeTruthy()
    expect(screen.getByText(/^L1·3\. second/)).toBeTruthy()
  })

  it('shows documents with status', () => {
    const docs = [
      { id: 1, task_id: 1, key: 'D1', name: 'Navi AR', about: 'Navi Finserv annual report FY26', status: 'verified', url: 'https://navi.com/ar.pdf' },
      { id: 2, task_id: 1, key: 'D2', name: 'CARE rationale', status: 'not_found' },
      { id: 3, task_id: 1, key: 'D3', name: 'ICRA note', status: 'planned' },
    ]
    render(<TaskChecklist task={{ ...task('running'), documents: docs }} />)
    expect(screen.getByText('✓ D1 Navi Finserv annual report FY26').closest('a').getAttribute('href')).toBe('https://navi.com/ar.pdf')
    expect(screen.getByText('✗ not found')).toBeTruthy()
    expect(screen.getByText('… searching')).toBeTruthy()
  })

  it('shows field progress and table', () => {
    const fields = [
      { id: 1, subtask_id: 2, name: 'Gross NPA %', period: 'FY26', status: 'found', accepted: 1, value: '2.1', unit: '%', source_key: 'D1', page: 112 },
      { id: 2, subtask_id: 2, name: 'Net NPA %', period: 'FY26', status: 'found', accepted: 0, value: '0.9', unit: '%', source_url: 'https://news.com/a' },
      { id: 3, subtask_id: 2, name: 'PCR %', period: 'FY26', status: 'unavailable', accepted: 1, note: 'not disclosed' },
      { id: 4, subtask_id: 2, name: 'CRAR %', period: 'FY26', status: 'open', accepted: 0 },
    ]
    render(<TaskChecklist task={{ ...task('running', [sub({})]), fields }} />)
    expect(screen.getByText('2/4 fields · round 0 · check-in at 5')).toBeTruthy()
    fireEvent.click(screen.getByText('Find prices'))
    expect(screen.getByText('2.1 %')).toBeTruthy()
    expect(screen.getByText('D1 p.112')).toBeTruthy()
    expect(screen.getByText('✓ accepted')).toBeTruthy()
    expect(screen.getByText('news.com')).toBeTruthy()
    expect(screen.getByText('unreviewed')).toBeTruthy()
    expect(screen.getByText('not public: not disclosed')).toBeTruthy()
    expect(screen.getByText('searching')).toBeTruthy()
  })

  it('old task without fields renders as before', () => {
    render(<TaskChecklist task={task('running', [sub({})])} />)
    expect(screen.getByText('round 0 · check-in at 5')).toBeTruthy()
    expect(screen.queryByText('Documents')).toBeNull()
  })
})
