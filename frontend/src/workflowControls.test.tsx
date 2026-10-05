import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import App, { applyEvents } from './App';
import { SlashPalette, slashCommands } from './components';
import { activeGoals, GoalsPanel } from './pages';
import { QuestionCard, QuestionPanel } from './questionPanel';
import { DEFAULT_SETTINGS } from './types';
import type { UserQuestion } from './types';

const request: UserQuestion = { id: 'question', run_id: 'run', chat_id: 'chat', status: 'pending', items: [
  { id: 'theme', header: 'Theme', question: 'Which theme should I use?', options: [{ label: 'Light', description: 'Light surfaces.' }, { label: 'System', description: 'Follow your computer.', recommended: true }] },
] };

describe('guided agent questions', () => {
  it('puts the recommendation first without selecting or submitting it for the user', () => {
    const call = vi.fn(); window.pywebview = { api: { call } };
    render(<QuestionCard request={request} onAnswered={vi.fn()} />);
    const radios = screen.getAllByRole('radio') as HTMLInputElement[];
    expect(radios[0].closest('label')?.textContent).toContain('SystemRecommended');
    expect(radios.every(radio => !radio.checked)).toBe(true);
    expect((screen.getByRole('button', { name: 'Continue' }) as HTMLButtonElement).disabled).toBe(true);
    expect(call).not.toHaveBeenCalled();
  });
  it('submits the exact option label and keeps permissions out of the answer payload', async () => {
    const call = vi.fn(async () => ({ ok: true })); window.pywebview = { api: { call } }; const answered = vi.fn();
    render(<QuestionCard request={request} onAnswered={answered} />);
    fireEvent.click(screen.getByRole('radio', { name: /System/ })); fireEvent.click(screen.getByRole('button', { name: 'Continue' }));
    await waitFor(() => expect(answered).toHaveBeenCalledOnce());
    expect(call).toHaveBeenCalledWith('answer_question', { question_id: 'question', answers: { theme: { option: 'System' } } });
  });
  it('waits for every question and permits a custom answer without retaining an old option', async () => {
    const call = vi.fn(async () => ({ ok: true })); window.pywebview = { api: { call } };
    render(<QuestionCard request={{ ...request, items: [...request.items, { id: 'layout', header: 'Layout', question: 'Which layout?', options: [{ label: 'Compact' }, { label: 'Roomy' }] }] }} onAnswered={vi.fn()} />);
    fireEvent.click(screen.getByRole('radio', { name: /System/ }));
    expect((screen.getByRole('button', { name: 'Continue' }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole('radio', { name: 'Roomy' }));
    fireEvent.click(screen.getAllByRole('radio', { name: /Other/ })[1]);
    fireEvent.change(screen.getByRole('textbox', { name: 'Your answer: Layout' }), { target: { value: '  Follow my screen size  ' } });
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }));
    await waitFor(() => expect(call).toHaveBeenCalledWith('answer_question', { question_id: 'question', answers: { theme: { option: 'System' }, layout: { text: 'Follow my screen size' } } }));
  });
  it('retains selections when answering fails or when the question card is collapsed', async () => {
    const call = vi.fn(async () => ({ error: 'Coordinator disconnected' })); window.pywebview = { api: { call } }; const answered = vi.fn();
    render(<QuestionCard request={request} onAnswered={answered} />);
    fireEvent.click(screen.getByRole('radio', { name: /Light/ })); fireEvent.click(screen.getByRole('button', { name: 'Continue' }));
    await screen.findByRole('alert'); expect(answered).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: /Forge needs your input/ })); fireEvent.click(screen.getByRole('button', { name: /Forge needs your input/ }));
    expect((screen.getByRole('radio', { name: /Light/ }) as HTMLInputElement).checked).toBe(true);
  });
  it('only displays pending questions for the selected conversation', async () => {
    const call = vi.fn(async () => ({ questions: [request, { ...request, id: 'other-chat', chat_id: 'another' }, { ...request, id: 'answered', status: 'answered' }] })); window.pywebview = { api: { call } };
    render(<QuestionPanel chatId="chat" active={false} notify={vi.fn()} />);
    await screen.findByRole('region', { name: 'Agent questions' });
    expect(screen.getAllByRole('region', { name: 'Agent questions' })).toHaveLength(1);
    expect(call).toHaveBeenCalledWith('pending_questions', { chat_id: 'chat' });
  });
});

describe('focused workflow navigation', () => {
  it('exposes only the canonical todo command in the slash palette', () => {
    expect(slashCommands.filter(([name]) => name === 'todo' || String(name) === 'to-do')).toHaveLength(1);
    render(<SlashPalette query="/to" select={vi.fn()} />);
    expect(screen.getAllByRole('option')).toHaveLength(1);
    expect(screen.getByRole('option').textContent).toContain('/todo');
  });
  it('hides completed, paused and orphaned goals while retaining runs waiting for a user', () => {
    const goals = [
      { id: 'running', chat_id: 'chat', status: 'running' }, { id: 'question', chat_id: 'chat', status: 'waiting_question' },
      { id: 'done', chat_id: 'chat', status: 'completed' }, { id: 'paused', chat_id: 'chat', status: 'paused' },
      { id: 'orphan', chat_id: 'deleted', status: 'running' }, { id: 'missing', status: 'running' },
    ];
    expect(activeGoals(goals, [{ id: 'chat', title: 'Current', model: 'fixture', project_id: null }]).map(goal => goal.id)).toEqual(['running', 'question']);
  });
  it('shows clear waiting and steering activity without finishing the run', () => {
    const initial = { run: { id: 'run' }, text: '', thinking: '', parts: [], tools: [], cursor: 0, status: 'running', finished: false };
    const waiting = applyEvents(initial, [{ seq: 1, type: 'question', question_id: 'question' }], { finished: false });
    expect(waiting.status).toBe('Waiting for your answers'); expect(waiting.finished).toBe(false);
    const steering = applyEvents(waiting, [{ seq: 2, type: 'steer', state: 'queued' }], { finished: false });
    expect(steering.status).toContain('Direction queued');
    expect(applyEvents(steering, [{ seq: 3, type: 'steer', status: 'applied' }], {}).status).toBe('Following your direction');
  });
  it('removes the selected checklist when its run completes and requests only active goals', async () => {
    let goals = [{ id: 'goal', chat_id: 'chat', run_id: 'run', title: 'Active implementation', status: 'running' }];
    const call = vi.fn(async (action: string) => action === 'goals' ? { goals } : { goal: goals[0], markdown: 'Current checklist checkpoint' });
    window.pywebview = { api: { call } };
    const props = { projectId: null, chats: [{ id: 'chat', model: 'fixture', title: 'Current', project_id: null }], notify: vi.fn(), onRun: vi.fn() };
    const view = render(<GoalsPanel {...props} version={0} />);
    fireEvent.click(await screen.findByRole('button', { name: /Active implementation/ }));
    await screen.findByText('Current checklist checkpoint');
    goals = [{ ...goals[0], status: 'completed' }]; view.rerender(<GoalsPanel {...props} version={1} />);
    await waitFor(() => expect(screen.queryByText('Current checklist checkpoint')).toBeNull());
    expect(screen.queryByRole('button', { name: /Active implementation/ })).toBeNull();
    expect(call).toHaveBeenCalledWith('goals', { project_id: null, active_only: true });
  });
});

function appBridge(steer: (data: Record<string, unknown>) => Promise<unknown>) {
  const chat = { id: 'chat', title: 'Steering fixture', project_id: null, model: 'fixture', messages: [{ role: 'user', content: 'Start work' }] };
  const run = { id: 'run', chat_id: chat.id, status: 'running' };
  const call = vi.fn(async (action: string, data: Record<string, unknown> = {}) => {
    if (action === 'bootstrap') return { settings: { ...DEFAULT_SETTINGS, model: 'fixture' }, projects: [], chats: [chat], runs: [run], capabilities: {} };
    if (action === 'get_chat') return chat;
    if (action === 'models') return { models: [{ name: 'fixture' }] };
    if (action === 'projects') return { projects: [] };
    if (action === 'chats') return { chats: [chat] };
    if (action === 'runs') return { runs: [run] };
    if (action === 'setup_status') return { first_run: false };
    if (action === 'pending_questions') return { questions: [] };
    if (action === 'poll') return { events: [], status: 'running', finished: false };
    if (action === 'run_steer') return steer(data);
    if (action === 'command') return { message: 'Current task status' };
    throw new Error(`Unexpected action ${action}`);
  });
  window.pywebview = { api: { call } }; return call;
}
async function startApp() { render(<App />); await act(async () => { window.dispatchEvent(new Event('pywebviewready')); }); await screen.findByRole('button', { name: 'Steer' }); }

describe('mid-run steering', () => {
  it('queues direction while leaving Stop available and preserves a newer draft during submission', async () => {
    let resolve!: (value: unknown) => void; const pending = new Promise(resolvePromise => { resolve = resolvePromise; });
    const call = appBridge(async () => pending); await startApp();
    const input = screen.getByRole('textbox', { name: 'Message Forge' }) as HTMLTextAreaElement;
    fireEvent.change(input, { target: { value: 'Use the existing component' } }); fireEvent.click(screen.getByRole('button', { name: 'Steer' }));
    expect(screen.getByRole('button', { name: 'Stop generation' })).toBeTruthy();
    fireEvent.change(input, { target: { value: 'Another direction being drafted' } });
    await act(async () => { resolve({ ok: true, status: 'queued' }); });
    expect(input.value).toBe('Another direction being drafted');
    expect(call).toHaveBeenCalledWith('run_steer', expect.objectContaining({ run_id: 'run', text: 'Use the existing component', client_id: expect.any(String) }));
    expect(call.mock.calls.filter(([action]) => action === 'start_chat')).toHaveLength(0);
  });
  it('retains a failed submission and reuses its idempotency key when retried with Enter', async () => {
    let attempts = 0; const call = appBridge(async () => ++attempts === 1 ? { error: 'Connection interrupted' } : { ok: true, status: 'queued' }); await startApp();
    const input = screen.getByRole('textbox', { name: 'Message Forge' }) as HTMLTextAreaElement;
    fireEvent.change(input, { target: { value: 'Ask before changing the design' } }); fireEvent.keyDown(input, { key: 'Enter' });
    await screen.findByText('Connection interrupted'); expect(input.value).toBe('Ask before changing the design');
    fireEvent.keyDown(input, { key: 'Enter' }); await waitFor(() => expect(input.value).toBe(''));
    const submissions = call.mock.calls.filter(([action]) => action === 'run_steer').map(([, data]) => data);
    expect(submissions).toHaveLength(2); expect(submissions[0]!.client_id).toBe(submissions[1]!.client_id);
  });
  it('routes active slash controls as commands and prevents starting an overlapping goal', async () => {
    const call = appBridge(async () => ({ ok: true })); await startApp();
    const input = screen.getByRole('textbox', { name: 'Message Forge' }) as HTMLTextAreaElement;
    fireEvent.change(input, { target: { value: '/status' } }); fireEvent.keyDown(input, { key: 'Enter' });
    await screen.findByText('Current task status');
    expect(call).toHaveBeenCalledWith('command', expect.objectContaining({ text: '/status', chat_id: 'chat' }));
    fireEvent.change(input, { target: { value: '/goal another task' } }); fireEvent.keyDown(input, { key: 'Enter' });
    await screen.findByText(/Pause this run before starting another workflow/);
    expect(input.value).toBe('/goal another task');
    expect(call.mock.calls.filter(([action]) => action === 'run_steer')).toHaveLength(0);
    expect(call.mock.calls.filter(([action]) => action === 'command')).toHaveLength(1);
  });
});
