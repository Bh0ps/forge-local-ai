import { useState } from 'react';
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { OpenRouterSettings } from '../openRouterSettings';
import { GoalReviewCard } from '../goalReviewCard';
import { activeGoals, GoalsPanel } from '../pages';
import App, { applyEvents } from '../App';
import { DEFAULT_SETTINGS } from '../types';
import type { Goal, Settings } from '../types';

const connection = { enabled: true, connected: true, remote_consent: true, data_collection: 'deny', models: [{ name: 'fixture/reviewer:free' }], account: { free_model_daily_requests: { used: 4, limit: 50, remaining: 46 } } };
function bridge(overrides: Record<string, unknown> = {}) {
  const responses: Record<string, unknown> = { openrouter_status: { provider: connection }, ...overrides };
  const call = vi.fn(async (action: string, data: Record<string, unknown> = {}) => {
    if (!(action in responses)) throw new Error(`Unexpected action: ${action}`);
    const result = responses[action]; return typeof result === 'function' ? result(data) : result;
  });
  window.pywebview = { api: { call } }; return { call, responses };
}
function settingsPage(initial: Partial<Settings> = {}) {
  const change = vi.fn(async (_value: Partial<Settings>) => {}); const notify = vi.fn();
  function Harness() {
    const [settings, setSettings] = useState({ ...DEFAULT_SETTINGS, model: 'local-fixture:27b', context: 131072, auto_delegate: false, goal_review_enabled: false, ...initial });
    return <OpenRouterSettings settings={settings} notify={notify} onChange={async delta => { await change(delta); setSettings(previous => ({ ...previous, ...delta })); }} />;
  }
  render(<Harness />); return { change, notify };
}

describe('OpenRouter helpers and independent review setup', () => {
  it('sets up helpers and review only on an explicit click without changing the main model or saved consent', async () => {
    const { call } = bridge({ openrouter_setup_agents: { agents: [{ id: 'researcher', name: 'Researcher' }, { id: 'assistant', name: 'Assistant' }], settings: { auto_delegate: true, goal_review_enabled: true, goal_review_model: 'openrouter/free', goal_review_max_revisions: 3, goal_review_context: 32768 } } });
    const { change, notify } = settingsPage();
    await screen.findByText('Account authenticated'); expect(call.mock.calls.map(([action]) => action)).toEqual(['openrouter_status','ai_workflow_status']);
    expect(screen.getByText(/Helpers and reviews share this quota/)).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: 'Set up helpers & reviewer' }));
    await waitFor(() => expect(call).toHaveBeenCalledWith('openrouter_setup_agents', {}));
    expect(change).toHaveBeenCalledWith({ auto_delegate: true, goal_review_enabled: true, goal_review_model: 'openrouter/free', goal_review_max_revisions: 3, goal_review_context: 32768 });
    expect(screen.getByRole('switch', { name: 'Verify goals before completion' }).getAttribute('aria-checked')).toBe('true');
    expect(notify).toHaveBeenCalledWith(expect.stringContaining('Your main model stays selected'));
    expect(call.mock.calls.some(([action]) => action === 'openrouter_save' || action === 'agent_save')).toBe(false);
  });
  it('requires saved consent and a tested connection before setup, and never echoes account metadata into a save', async () => {
    const { call } = bridge({ openrouter_status: { provider: { ...connection, enabled: false, remote_consent: false } }, openrouter_save: { provider: { ...connection, connected: false } }, openrouter_test: { provider: connection } });
    settingsPage(); await screen.findByText('Account authenticated');
    const setup = screen.getByRole('button', { name: 'Set up helpers & reviewer' }) as HTMLButtonElement;
    expect(setup.disabled).toBe(true);
    await userEvent.click(screen.getByRole('switch', { name: 'Allow assigned context to leave this computer' }));
    await userEvent.click(screen.getByRole('switch', { name: 'Enable free OpenRouter agents' }));
    expect(setup.disabled).toBe(true); expect(call).toHaveBeenCalledTimes(2);
    await userEvent.click(screen.getByRole('button', { name: 'Save connection' }));
    await waitFor(() => expect(call).toHaveBeenCalledWith('openrouter_save', { enabled: true, remote_consent: true, data_collection: 'deny' }));
    expect(setup.disabled).toBe(true);
    await userEvent.click(screen.getByRole('button', { name: 'Connect / Test' }));
    await waitFor(() => expect(setup.disabled).toBe(false));
    expect(call.mock.calls.some(([action]) => action === 'openrouter_setup_agents')).toBe(false);
  });
  it('keeps the independent reviewer separate from delegation and persists only its selected control', async () => {
    bridge(); const { change } = settingsPage(); await screen.findByText('Account authenticated');
    await userEvent.click(screen.getByRole('switch', { name: 'Verify goals before completion' }));
    expect(change).toHaveBeenLastCalledWith({ goal_review_enabled: true });
    expect(screen.getByRole('switch', { name: 'Automatic delegation' }).getAttribute('aria-checked')).toBe('false');
    await userEvent.selectOptions(screen.getByLabelText('Reviewer model'), 'fixture/reviewer:free');
    expect(change).toHaveBeenLastCalledWith({ goal_review_model: 'fixture/reviewer:free' });
    await userEvent.selectOptions(screen.getByLabelText('Correction passes'), '2');
    expect(change).toHaveBeenLastCalledWith({ goal_review_max_revisions: 2 });
    await userEvent.selectOptions(screen.getByLabelText('Review context'), '16384');
    expect(change).toHaveBeenLastCalledWith({ goal_review_context: 16384 });
  });
  it('surfaces setup errors and does not claim helpers or review are enabled', async () => {
    bridge({ openrouter_setup_agents: { error: 'Free model quota exhausted. Try again later.' } });
    const { change, notify } = settingsPage(); await screen.findByText('Account authenticated');
    await userEvent.click(screen.getByRole('button', { name: 'Set up helpers & reviewer' }));
    expect(await screen.findByRole('alert')).toHaveProperty('textContent', 'Free model quota exhausted. Try again later.');
    expect(change).not.toHaveBeenCalled(); expect(notify).not.toHaveBeenCalled();
  });
  it('retains saved reviewer context values between the quick presets', async () => {
    bridge(); settingsPage({ goal_review_context: 98304 }); await screen.findByText('Account authenticated');
    expect((screen.getByLabelText('Review context') as HTMLSelectElement).value).toBe('98304');
    expect(screen.getByRole('option', { name: '96K' })).toBeTruthy();
  });
});

describe('durable goal review visibility', () => {
  const chat = { id: 'chat-fixture', project_id: null, title: 'Fixture', model: 'local-fixture:27b' };
  const goal: Goal = { id: 'goal-fixture', chat_id: chat.id, title: 'Build fixture', status: 'running', review: { status: 'reviewing', model: 'openrouter/free', attempt: 1 } };
  it('retains unverified review recovery while hiding completed, ordinary paused and orphaned goals', () => {
    const rows: Goal[] = [goal, { ...goal, id: 'fix', status: 'paused', review: { status: 'needs_changes' } }, { ...goal, id: 'error', status: 'paused', review: { status: 'error' } }, { ...goal, id: 'evidence', status: 'paused', review: { status: 'insufficient_evidence' } }, { ...goal, id: 'done', status: 'completed', review: { status: 'complete' } }, { ...goal, id: 'ordinary-pause', status: 'paused', review: undefined }, { ...goal, id: 'orphan', chat_id: 'deleted-chat' }];
    expect(activeGoals(rows, [chat]).map(item => item.id)).toEqual(['goal-fixture','fix','error','evidence']);
  });
  it('refreshes an open goal review after polling and leaves paused unverified goals resumable', async () => {
    const { call, responses } = bridge({ goals: { goals: [goal] }, goal_get: { goal, markdown: '1. [x] Build fixture' }, goal_resume: { id: 'resumed', status: 'running' } });
    const notify = vi.fn(); const onRun = vi.fn(); const chats = [chat];
    const { rerender } = render(<GoalsPanel projectId={null} chats={chats} version={0} notify={notify} onRun={onRun} />);
    await userEvent.click(await screen.findByRole('button', { name: /Build fixture/ }));
    expect(await screen.findByText('Reviewing completion')).toBeTruthy();
    responses.goals = { goals: [{ ...goal, status: 'paused', markdown: '1. [x] Build fixture\n2. [ ] Save missing test evidence', review: { status: 'insufficient_evidence', summary: 'A test result is missing.', feedback: ['Run the regression test and save its output.'], attempt: 1 } }] };
    rerender(<GoalsPanel projectId={null} chats={chats} version={1} notify={notify} onRun={onRun} />);
    expect(await screen.findByText('More evidence needed')).toBeTruthy();
    expect(screen.getByText('Run the regression test and save its output.')).toBeTruthy();
    expect(screen.getByText('[ ] Save missing test evidence')).toBeTruthy();
    expect(screen.getByText(/Goal paused without verified completion/)).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: 'Resume' }));
    await waitFor(() => expect(call).toHaveBeenCalledWith('goal_resume', { id: goal.id }));
    expect(onRun).toHaveBeenCalledWith({ id: 'resumed', status: 'running' });
  });
  it('deduplicates replayed review events and preserves the verdict on the main run', () => {
    const initial = { run: { id: 'local-main' }, text: 'Built fixture.', thinking: '', parts: [], tools: [], cursor: 0, status: 'Working', finished: false };
    const review = { status: 'needs_changes' as const, verdict: 'needs_changes' as const, summary: 'Fix the empty input.', feedback: ['Add an empty-input check.'], review_run_id: 'remote-reviewer', attempt: 1 };
    const reviewed = applyEvents(initial, [{ seq: 1, type: 'goal_review', review }], {});
    expect(reviewed.run.review).toEqual(review); expect(reviewed.status).toBe('Applying reviewer feedback');
    const replayed = applyEvents(reviewed, [{ seq: 1, type: 'goal_review', review: { status: 'complete' } }], {});
    expect(replayed.run.review).toEqual(review);
    const done = applyEvents(replayed, [{ seq: 2, type: 'goal_review', review: { status: 'complete', verdict: 'complete', summary: 'The saved tests cover the goal.' } }, { seq: 3, type: 'done' }], { finished: true, status: 'completed' });
    expect(done.run.review?.status).toBe('complete'); expect(done.finished).toBe(true);
  });
  it('restores a live independent review after reconnect with Stop and Activity available', async () => {
    const run = { id: 'local-main', chat_id: chat.id, status: 'reviewing', mode: 'goal', model: 'local-fixture:27b', review: goal.review };
    bridge({ bootstrap: { settings: { ...DEFAULT_SETTINGS, model: 'local-fixture:27b' }, projects: [], chats: [chat], runs: [run], capabilities: {} }, setup_status: { first_run: false }, get_chat: { ...chat, messages: [{ id: 1, role: 'user', content: '/goal Build fixture' }] }, models: { models: [{ name: 'local-fixture:27b' }] }, projects: { projects: [] }, chats: { chats: [chat] }, runs: { runs: [run] }, poll: { events: [], finished: false, status: 'reviewing' } });
    render(<App />); await act(async () => { window.dispatchEvent(new Event('pywebviewready')); });
    expect(await screen.findByRole('button', { name: 'Stop generation' })).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: 'Open workspace panel' }));
    expect(await screen.findByText('Reviewing completion')).toBeTruthy();
    expect(screen.queryByText('All quiet')).toBeNull();
  });
  it('never describes an unavailable review as verified completion', () => {
    render(<GoalReviewCard paused review={{ status: 'error', summary: 'Free quota exhausted.' }} />);
    expect(screen.getByText('Review unavailable')).toBeTruthy(); expect(screen.getByText(/without verified completion/)).toBeTruthy();
    expect(screen.queryByText('Completion verified')).toBeNull();
  });
  it('stops the reviewing spinner after interruption and shows a resumable unverified pause', () => {
    render(<GoalReviewCard paused review={{ status: 'reviewing', summary: 'Review was interrupted.' }} />);
    expect(screen.getByText('Review paused')).toBeTruthy(); expect(document.querySelector('.goal-review-card .spin')).toBeNull();
    expect(screen.getByText(/without verified completion/)).toBeTruthy();
  });
  it('labels an explicitly disabled reviewer without suggesting verification or a review failure', () => {
    render(<GoalReviewCard paused review={{ status: 'disabled', summary: 'Independent review disabled; local completion checks apply.' }} />);
    expect(screen.getByText('Independent review off')).toBeTruthy();
    expect(screen.queryByText('Completion verified')).toBeNull(); expect(screen.queryByText(/without verified completion/)).toBeNull();
  });
});
