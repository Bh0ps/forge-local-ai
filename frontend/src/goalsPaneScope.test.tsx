import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { goalInProject, GoalsPanel } from './pages';
import type { Chat, Goal } from './types';

const chats: Chat[] = [
  { id: 'chat-a', project_id: 'project-a', title: 'Alpha chat', model: 'fixture' },
  { id: 'chat-b', project_id: 'project-b', title: 'Bravo chat', model: 'fixture' },
  { id: 'chat-global', project_id: null, title: 'Unassigned chat', model: 'fixture' },
];
const alpha = { id: 'goal-a', project_id: 'project-a', chat_id: 'chat-a', run_id: 'run-a', title: 'Alpha goal', status: 'running', execution_mode: 'guided' };
const another = { ...alpha, id: 'goal-a2', title: 'Second alpha goal', run_id: 'run-a2' };
const bravo = { ...alpha, id: 'goal-b', project_id: 'project-b', chat_id: 'chat-b', run_id: 'run-b', title: 'Bravo goal' };
const global = { ...alpha, id: 'goal-global', project_id: null, chat_id: 'chat-global', title: 'Unassigned goal' };
function deferred<T = unknown>() {
  let resolve!: (value: T) => void, reject!: (reason: Error) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function bridge(overrides: Record<string, unknown> = {}) {
  const responses: Record<string, unknown> = { goals: { goals: [alpha, another, bravo, global] }, ...overrides };
  const call = vi.fn(async (action: string, data: Record<string, unknown> = {}) => {
    if (!(action in responses)) throw new Error(`Unexpected synthetic action: ${action}`);
    const result = responses[action]; return typeof result === 'function' ? result(data) : result;
  });
  window.pywebview = { api: { call } }; return { responses, call };
}
const props = { projectId: 'project-a', chats, version: 0, notify: vi.fn(), onRun: vi.fn() };
const row = (name = 'Alpha goal') => screen.getByRole('button', { name: new RegExp(`^${name}`) });

describe('goal detail pane scope and request ownership', () => {
  it('filters old global responses by the known chat project and authoritative goal project', async () => {
    const mismatch = { ...alpha, id: 'mismatch', title: 'Mismatched owner', project_id: 'project-b' };
    const orphan = { ...alpha, id: 'orphan', title: 'Orphaned goal', chat_id: 'missing-chat' };
    const { call } = bridge({ goals: { goals: [alpha, bravo, global, mismatch, orphan] } });
    const view = render(<GoalsPanel {...props} />);
    await screen.findByRole('button', { name: /^Alpha goal/ });
    expect(screen.queryByRole('button', { name: /Bravo|Unassigned|Mismatched|Orphaned/ })).toBeNull();
    expect(call).toHaveBeenCalledWith('goals', { project_id: 'project-a', active_only: true });
    view.rerender(<GoalsPanel {...props} projectId={null} />);
    await screen.findByRole('button', { name: /^Unassigned goal/ });
    expect(screen.queryByRole('button', { name: /Alpha|Bravo/ })).toBeNull();
    expect(call).toHaveBeenCalledWith('goals', { project_id: null, active_only: true });
    expect(goalInProject({ id: alpha.id, chat_id: alpha.chat_id, status: alpha.status }, 'project-a', chats)).toBe(true);
    expect(goalInProject(mismatch, 'project-a', chats)).toBe(false);
  });

  it('shows loading before an honest empty state and offers retry for a failed list', async () => {
    const pending = deferred(); const { responses } = bridge({ goals: pending.promise });
    render(<GoalsPanel {...props} />);
    expect(screen.getByText('Loading goals…')).toBeTruthy(); expect(screen.queryByText('No active goals')).toBeNull();
    await act(async () => { pending.reject(new Error('Synthetic goal listing unavailable')); });
    await screen.findByRole('alert'); expect(screen.queryByText('No active goals')).toBeNull();
    responses.goals = { goals: [] }; await userEvent.click(screen.getByRole('button', { name: 'Retry' }));
    await screen.findByText('No active goals'); expect(screen.queryByText('Loading goals…')).toBeNull();
  });

  it.each(['resolve', 'reject'] as const)('ignores the older selected goal that settles by %s after a newer selection', async outcome => {
    const first = deferred(), second = deferred(); const notify = vi.fn();
    const { call } = bridge({ goal_get: ({ id }: Record<string, unknown>) => id === alpha.id ? first.promise : second.promise });
    render(<GoalsPanel {...props} notify={notify} />);
    await userEvent.click(await screen.findByRole('button', { name: /^Alpha goal/ }));
    expect(screen.getByText('Loading goal…')).toBeTruthy();
    await userEvent.click(row('Second alpha goal'));
    await act(async () => { second.resolve({ goal: another, markdown: 'Second selection checklist' }); });
    await screen.findByText('Second selection checklist');
    await act(async () => { if (outcome === 'resolve') first.resolve({ goal: alpha, markdown: 'Old selection checklist' }); else first.reject(new Error('Old selection failure')); });
    expect(screen.queryByText('Old selection checklist')).toBeNull(); expect(notify).not.toHaveBeenCalled();
    expect(call).toHaveBeenCalledWith('goal_get', { id: another.id, project_id: 'project-a' });
  });

  it('retires a pending read when closed and restores focus to its goal row', async () => {
    const pending = deferred(); bridge({ goal_get: pending.promise }); render(<GoalsPanel {...props} />);
    const trigger = await screen.findByRole('button', { name: /^Alpha goal/ }); await userEvent.click(trigger);
    await userEvent.click(screen.getByRole('button', { name: 'Close goal detail' }));
    expect(document.activeElement).toBe(trigger);
    await act(async () => { pending.resolve({ goal: alpha, markdown: 'Closed detail must stay closed' }); });
    expect(screen.queryByText('Closed detail must stay closed')).toBeNull(); expect(screen.queryByRole('button', { name: 'Export' })).toBeNull();
  });

  it('hides old selected details immediately on project navigation and ignores pending old-scope reads', async () => {
    const pending = deferred(); const { responses } = bridge({ goal_get: { goal: alpha, markdown: 'Alpha detail' } });
    const view = render(<GoalsPanel {...props} />);
    await userEvent.click(await screen.findByRole('button', { name: /^Alpha goal/ })); await screen.findByText('Alpha detail');
    responses.goal_get = pending.promise; await userEvent.click(row('Second alpha goal'));
    view.rerender(<GoalsPanel {...props} projectId="project-b" />);
    expect(screen.queryByText('Alpha detail')).toBeNull(); expect(screen.queryByRole('button', { name: 'Export' })).toBeNull();
    await act(async () => { pending.resolve({ goal: another, markdown: 'Late foreign checklist' }); });
    await screen.findByRole('button', { name: /^Bravo goal/ }); expect(screen.queryByText('Late foreign checklist')).toBeNull();
  });

  it('rejects a mismatched detail response even when the old backend returns it successfully', async () => {
    const notify = vi.fn(); bridge({ goal_get: { goal: bravo, markdown: 'Foreign private checklist' } });
    render(<GoalsPanel {...props} notify={notify} />);
    await userEvent.click(await screen.findByRole('button', { name: /^Alpha goal/ }));
    await waitFor(() => expect(notify).toHaveBeenCalledWith('This goal is no longer available in this project.'));
    expect(screen.queryByText('Foreign private checklist')).toBeNull(); expect(screen.queryByRole('button', { name: 'Export' })).toBeNull();
  });

  it('ignores a delayed response after unmount without affecting a reopened pane', async () => {
    const pending = deferred(); const notify = vi.fn(); const { responses } = bridge({ goal_get: pending.promise });
    const old = render(<GoalsPanel {...props} notify={notify} />); await userEvent.click(await screen.findByRole('button', { name: /^Alpha goal/ })); old.unmount();
    responses.goal_get = { goal: bravo, markdown: 'Reopened Bravo checklist' };
    render(<GoalsPanel {...props} projectId="project-b" notify={notify} />); await userEvent.click(await screen.findByRole('button', { name: /^Bravo goal/ }));
    await screen.findByText('Reopened Bravo checklist'); await act(async () => { pending.reject(new Error('Old closed pane failure')); });
    expect(notify).not.toHaveBeenCalled(); expect(screen.getByText('Reopened Bravo checklist')).toBeTruthy();
  });

  it('serializes mutations, keeps paused goals resumable and binds export to the current owner', async () => {
    const pending = deferred(); const paused = { ...alpha, status: 'paused' };
    const { call, responses } = bridge({ goal_get: { goal: alpha, markdown: 'Current checklist' }, goal_pause: pending.promise, goal_export: { path: 'FORGE_TODO.md' } });
    render(<GoalsPanel {...props} />); await userEvent.click(await screen.findByRole('button', { name: /^Alpha goal/ })); await screen.findByText('Current checklist');
    fireEvent.click(screen.getByRole('button', { name: 'Pause' })); fireEvent.click(screen.getByRole('button', { name: 'Pause' }));
    expect(call.mock.calls.filter(([action]) => action === 'goal_pause')).toHaveLength(1);
    for (const name of ['Pause', 'Export', 'Close goal detail']) expect((screen.getByRole('button', { name }) as HTMLButtonElement).disabled).toBe(true);
    responses.goals = { goals: [paused] }; responses.goal_get = { goal: paused, markdown: 'Paused checklist' };
    await act(async () => { pending.resolve({ ok: true }); }); await screen.findByText('Paused checklist');
    expect((screen.getByRole('button', { name: 'Pause' }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole('button', { name: 'Resume' }) as HTMLButtonElement).disabled).toBe(false);
    await userEvent.click(screen.getByRole('button', { name: 'Export' }));
    expect(call).toHaveBeenCalledWith('goal_export', { id: alpha.id, project_id: 'project-a' });
  });

  it('does not reopen or refocus old-scope work when a resume completes after navigation', async () => {
    const pending = deferred(); const paused = { ...alpha, status: 'paused' }; const onRun = vi.fn();
    const { call } = bridge({ goals: { goals: [paused, bravo] }, goal_get: { goal: paused, markdown: 'Paused Alpha checklist' }, goal_resume: pending.promise });
    const view = render(<GoalsPanel {...props} onRun={onRun} />); await userEvent.click(await screen.findByRole('button', { name: /^Alpha goal/ })); await screen.findByText('Paused Alpha checklist');
    await userEvent.click(screen.getByRole('button', { name: 'Resume' })); view.rerender(<GoalsPanel {...props} projectId="project-b" onRun={onRun} />);
    await screen.findByRole('button', { name: /^Bravo goal/ }); const reads = call.mock.calls.filter(([action]) => action === 'goals').length;
    await act(async () => { pending.resolve({ id: 'resumed-alpha', status: 'running' }); });
    expect(onRun).not.toHaveBeenCalled(); expect(screen.queryByText('Paused Alpha checklist')).toBeNull();
    expect(call.mock.calls.filter(([action]) => action === 'goals')).toHaveLength(reads);
  });

  it('does not offer export for unassigned goals and explains the needed project', async () => {
    bridge({ goal_get: { goal: global, markdown: 'Unassigned checklist' } }); render(<GoalsPanel {...props} projectId={null} />);
    await userEvent.click(await screen.findByRole('button', { name: /^Unassigned goal/ })); await screen.findByText('Unassigned checklist');
    expect((screen.getByRole('button', { name: 'Export' }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText('Connect this goal to a project to export its checklist.')).toBeTruthy();
  });

  it('keeps fresh Markdown and conflict after a stale cached list document reappears', async () => {
    const saved = { ...alpha, revision: 1, markdown_hash: 'saved-hash', markdown: 'Old cached Markdown' };
    const { responses } = bridge({ goals: { goals: [saved] }, goal_get: { goal: saved, markdown: 'Fresh external Markdown', conflict: true } });
    const view = render(<GoalsPanel {...props} />); await userEvent.click(await screen.findByRole('button', { name: /^Alpha goal/ })); await screen.findByText('Fresh external Markdown');
    responses.goals = { goals: [saved] }; view.rerender(<GoalsPanel {...props} version={1} />);
    await waitFor(() => expect(screen.queryByText('Loading goals…')).toBeNull());
    expect(screen.getByText('Fresh external Markdown')).toBeTruthy(); expect(screen.queryByText('Old cached Markdown')).toBeNull();
    expect(screen.getByText('Checklist changed outside Forge')).toBeTruthy(); expect((screen.getByRole('button', { name: 'Resume' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('refreshes changed revisions without discarding the current document or retrying failed reads endlessly', async () => {
    const saved = { ...alpha, revision: 1, markdown_hash: 'hash-one' }; const changed = { ...saved, revision: 2, markdown_hash: 'hash-two', markdown: 'Stale cached revision-two text' };
    const pending = deferred(); const { responses, call } = bridge({ goals: { goals: [saved] }, goal_get: { goal: saved, markdown: 'Revision-one document' } });
    const view = render(<GoalsPanel {...props} />); await userEvent.click(await screen.findByRole('button', { name: /^Alpha goal/ })); await screen.findByText('Revision-one document');
    responses.goals = { goals: [changed] }; responses.goal_get = pending.promise; view.rerender(<GoalsPanel {...props} version={1} />);
    await screen.findByText('Refreshing checklist…'); expect(screen.getByText('Revision-one document')).toBeTruthy();
    await act(async () => { pending.reject(new Error('Synthetic detail refresh unavailable')); });
    await screen.findByRole('alert'); const reads = call.mock.calls.filter(([action]) => action === 'goal_get').length;
    view.rerender(<GoalsPanel {...props} version={2} />); await act(async () => {});
    expect(call.mock.calls.filter(([action]) => action === 'goal_get')).toHaveLength(reads);
    responses.goal_get = { goal: changed, markdown: 'Fresh revision-two document' }; await userEvent.click(screen.getByRole('button', { name: 'Retry' }));
    await screen.findByText('Fresh revision-two document'); expect(screen.queryByText('Stale cached revision-two text')).toBeNull();
  });

  it('does not downgrade the current run when a task acceptance reply arrives for a superseded run', async () => {
    const original = { ...alpha, status: 'paused', revision: 1, markdown_hash: 'original', tasks: [{ id: 'task', text: 'Inspect fixture', status: 'pending' }] };
    const latest = { ...original, run_id: 'run-current', revision: 3, markdown_hash: 'current' };
    const pending = deferred(); const { responses, call } = bridge({ goals: { goals: [original] }, goal_get: { goal: original, markdown: 'Original run checklist' }, goal_task_accept: pending.promise, goal_replan: { goal: latest } });
    const view = render(<GoalsPanel {...props} />); await userEvent.click(await screen.findByRole('button', { name: /^Alpha goal/ })); await screen.findByText('Original run checklist');
    await userEvent.click(screen.getByText('Inspect fixture')); await userEvent.click(screen.getByRole('button', { name: 'Accept checked task' }));
    await userEvent.type(screen.getByRole('textbox', { name: 'What did you check?' }), 'Synthetic inspected result');
    await userEvent.click(screen.getByRole('checkbox', { name: 'I reviewed this task\'s current result' })); await userEvent.click(screen.getByRole('button', { name: 'Record acceptance' }));
    responses.goals = { goals: [latest] }; responses.goal_get = { goal: latest, markdown: 'Current run checklist' }; view.rerender(<GoalsPanel {...props} version={1} />);
    await screen.findByText('Current run checklist'); await act(async () => { pending.resolve({ goal: { ...original, revision: 2, markdown: 'Old accepted checklist' } }); });
    expect(screen.queryByText('Old accepted checklist')).toBeNull(); expect(screen.getByText('Current run checklist')).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: 'Request new guidance' }));
    await waitFor(() => expect(call).toHaveBeenCalledWith('goal_replan', expect.objectContaining({ run_id: latest.run_id, expected_revision: 3 })));
  });

  it('retires an older detail read when an already-started acceptance publishes newer goal state', async () => {
    const original = { ...alpha, status: 'paused', revision: 1, markdown_hash: 'one', tasks: [{ id: 'task', text: 'Inspect fixture', status: 'pending' }] };
    const interim = { ...original, revision: 2, markdown_hash: 'two' }, accepted = { ...original, revision: 3, markdown_hash: 'three', markdown: 'Accepted current checklist' };
    const acceptance = deferred(), detail = deferred(); const { responses } = bridge({ goals: { goals: [original] }, goal_get: { goal: original, markdown: 'Original checklist' }, goal_task_accept: acceptance.promise });
    const view = render(<GoalsPanel {...props} />); await userEvent.click(await screen.findByRole('button', { name: /^Alpha goal/ })); await screen.findByText('Original checklist');
    await userEvent.click(screen.getByText('Inspect fixture')); await userEvent.click(screen.getByRole('button', { name: 'Accept checked task' }));
    await userEvent.type(screen.getByRole('textbox', { name: 'What did you check?' }), 'Synthetic current result'); await userEvent.click(screen.getByRole('checkbox', { name: 'I reviewed this task\'s current result' })); await userEvent.click(screen.getByRole('button', { name: 'Record acceptance' }));
    responses.goals = { goals: [interim] }; responses.goal_get = detail.promise; view.rerender(<GoalsPanel {...props} version={1} />); await screen.findByText('Refreshing checklist…');
    responses.goals = { goals: [accepted] }; await act(async () => { acceptance.resolve({ goal: accepted }); }); await screen.findByText('Accepted current checklist');
    await act(async () => { detail.resolve({ goal: interim, markdown: 'Obsolete detail checklist', conflict: true }); });
    expect(screen.getByText('Accepted current checklist')).toBeTruthy(); expect(screen.queryByText('Obsolete detail checklist')).toBeNull(); expect(screen.queryByText('Checklist changed outside Forge')).toBeNull(); expect(screen.queryByText('Refreshing checklist…')).toBeNull();
  });

  it('preserves a newer list revision received during opening and refreshes the authoritative document', async () => {
    const original = { ...alpha, revision: 1, markdown_hash: 'one' }, newer = { ...alpha, status: 'paused', revision: 2, markdown_hash: 'two' };
    const pending = deferred(); const { responses, call } = bridge({ goals: { goals: [original] }, goal_get: pending.promise });
    const view = render(<GoalsPanel {...props} />); await userEvent.click(await screen.findByRole('button', { name: /^Alpha goal/ }));
    responses.goals = { goals: [newer] }; responses.goal_get = { goal: newer, markdown: 'Newest authoritative checklist' }; view.rerender(<GoalsPanel {...props} version={1} />);
    await screen.findByRole('button', { name: /^Alpha goalpaused/ }); await act(async () => { pending.resolve({ goal: original, markdown: 'Older opened checklist' }); });
    await screen.findByText('Newest authoritative checklist'); expect(screen.queryByText('Older opened checklist')).toBeNull();
    expect((screen.getByRole('button', { name: 'Resume' }) as HTMLButtonElement).disabled).toBe(false); expect(call.mock.calls.filter(([action]) => action === 'goal_get')).toHaveLength(2);
  });
});
