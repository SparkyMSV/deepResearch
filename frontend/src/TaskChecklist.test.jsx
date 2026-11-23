import { render, screen, fireEvent, cleanup } from '@testing-library/react'
import { describe, it, expect, afterEach } from 'vitest'
import TaskChecklist from './TaskChecklist.jsx'

afterEach(cleanup)
const task = (status, subtasks = []) => ({ id: 1, objective: 'x', status, subtasks })
const sub = (o) => ({ id: 2, task_id: 1, objective: 'Find prices', status: 'running', round: 0, loop: 0, restarts: 0, rounds: [], ...o })

describe('TaskChecklist', () => {
  it.each([
    ['paused_quota', 'Paused — quota exhausted, resumes after reset'],
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
    expect(screen.getByText('paused — quota exhausted, resumes after reset')).toBeTruthy()
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
})
