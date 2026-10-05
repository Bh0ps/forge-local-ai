import { createElement } from 'react';
import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import App, { applyEvents, reconcileRunMessages } from './App';
import { DEFAULT_SETTINGS } from './types';
import type { Message, Run, RunEvent, SavedChat } from './types';
const initial = () => ({ run: { id: 'run' }, text: '', thinking: '', parts: [], tools: [], cursor: 0, status: 'running', finished: false });
describe('durable activity and speed events', () => {
  it('updates a web fetch card in place before a completed turn', () => {
    const live = applyEvents(initial(), [{ seq: 1, type: 'tool', name: 'web_fetch', invocation_id: 'a', state: 'running', arguments: { url: 'https://example.com' } }, { seq: 2, type: 'tool', name: 'web_fetch', invocation_id: 'a', state: 'done', result: { artifact: 'saved' } }, { seq: 3, type: 'done', status: 'completed' }], { finished: true, status: 'completed' });
    expect(live.tools).toHaveLength(1); expect(live.tools[0].state).toBe('done'); expect(live.tools[0].arguments).toEqual({ url: 'https://example.com' }); expect(live.finished).toBe(true);
  });
  it('keeps different calls to the same tool distinct and deduplicates replay', () => {
    let live = applyEvents(initial(), [{ seq: 1, type: 'tool', name: 'web_fetch', invocation_id: 'a', state: 'running' }, { seq: 2, type: 'tool', name: 'web_fetch', invocation_id: 'b', state: 'running' }], {});
    live = applyEvents(live, [{ seq: 2, type: 'tool', name: 'web_fetch', invocation_id: 'b', state: 'running' }, { seq: 3, type: 'tool', name: 'web_fetch', invocation_id: 'a', state: 'done' }], {});
    expect(live.tools).toHaveLength(2); expect(live.tools.map(t => t.state)).toEqual(['done', 'running']);
  });
  it('uses backend speed rather than counting text delivered by polling', () => {
    const live = applyEvents(initial(), [{ seq: 1, type: 'token', text: 'Buffered output '.repeat(1000) }, { seq: 2, type: 'stream_speed', tps: 20, estimated: true }], {});
    expect(live.speed).toBe(20); expect(live.speedEstimated).toBe(true);
    const complete = applyEvents(live, [{ seq: 3, type: 'generation', tps: 19.8, estimated: false }], {});
    expect(complete.speed).toBe(19.8); expect(complete.speedEstimated).toBe(false);
  });
  it('settles an interrupted tool without pretending its side effect completed', () => {
    const live = applyEvents(initial(), [{ seq: 1, type: 'tool', name: 'run_command', invocation_id: 'a', state: 'running' }, { seq: 2, type: 'outcome_unknown', name: 'run_command', invocation_id: 'a' }, { seq: 3, type: 'done', status: 'paused' }], { finished: true, status: 'paused' });
    expect(live.tools).toHaveLength(1); expect(live.tools[0].state).toBe('Outcome unknown');
    const interrupted = applyEvents(initial(), [{ seq: 1, type: 'tool', name: 'web_fetch', invocation_id: 'b', state: 'running' }], { finished: true, status: 'interrupted' });
    expect(interrupted.tools[0].state).toBe('Interrupted');
  });
  it('treats current poll completion as authoritative during resumed history replay', () => {
    const events: RunEvent[] = [
      { seq: 999, type: 'tool', name: 'web_fetch', invocation_id: 'active-call', state: 'running' },
      { seq: 1000, type: 'done', status: 'paused', cancelled: true },
    ];
    const live = applyEvents(initial(), events, { next_cursor: 1000, status: 'running', finished: false });
    expect(live.finished).toBe(false); expect(live.status).toBe('running'); expect(live.cursor).toBe(1000);
    expect(live.tools[0].state).toBe('running');
    const resumed = applyEvents(live, [{ seq: 1001, type: 'token', text: 'Continued output.' }], { next_cursor: 1001, status: 'running', finished: false });
    expect(resumed.text).toBe('Continued output.'); expect(resumed.finished).toBe(false);
    expect(applyEvents(resumed, [{ seq: 1002, type: 'done' }], { status: 'completed', finished: true }).finished).toBe(true);
  });
  it('keeps polling terminal history until the coordinator finishes its final page', () => {
    const live = applyEvents(initial(), [{ seq: 1000, type: 'done' }], { status: 'completed', finished: false, next_cursor: 1000 });
    expect(live.finished).toBe(false);
    const final = applyEvents(live, [{ seq: 1001, type: 'token', text: 'Final persisted output.' }], { status: 'completed', finished: true, next_cursor: 1001 });
    expect(final.finished).toBe(true); expect(final.text).toBe('Final persisted output.'); expect(final.cursor).toBe(1001);
    expect(applyEvents(initial(), [{ seq: 1, type: 'done' }], {}).finished).toBe(true);
  });
});

afterEach(() => { cleanup(); vi.useRealTimers(); });

function coordinator(run: Run, poll: (after: number) => unknown, activeRun = run, messages: Message[] = [{ role: 'assistant', content: 'Saved earlier turn.' }]) {
  const chat = { id: 'chat', title: 'Continuity fixture', project_id: null, model: 'fixture', messages };
  const call = vi.fn(async (action: string, data: Record<string, unknown> = {}) => {
    if (action === 'bootstrap') return { settings: { ...DEFAULT_SETTINGS, model: 'fixture' }, projects: [], chats: [chat], runs: [run], capabilities: {} };
    if (action === 'models') return { models: [{ name: 'fixture' }] };
    if (action === 'projects') return { projects: [] };
    if (action === 'chats') return { chats: [chat] };
    if (action === 'get_chat') return chat;
    if (action === 'setup_status') return { first_run: false };
    if (action === 'runs') return { runs: [activeRun] };
    if (action === 'poll') return { chat_id: chat.id, ...poll(Number(data.after)) as object };
    throw new Error(`Unexpected coordinator action: ${action}`);
  });
  window.pywebview = { api: { call } };
  return call;
}
async function advance(milliseconds: number) { await act(async () => { await vi.advanceTimersByTimeAsync(milliseconds); }); }
async function openApp() {
  render(createElement(App));
  await act(async () => { window.dispatchEvent(new Event('pywebviewready')); });
}

describe('run polling continuity in the app', () => {
  it('shows a persisted bootstrap round once while preserving identical pre-request history', async () => {
    vi.useFakeTimers();
    const repeated = 'This assistant round is already persisted.';
    const run = { id: 'run', chat_id: 'chat', status: 'running', request_message_id: 10 };
    coordinator(run, () => ({ events: [
      { seq: 1, type: 'round', number: 1 }, { seq: 2, type: 'token', text: repeated },
      { seq: 3, type: 'tool', name: 'web_fetch', invocation_id: 'saved-call', state: 'done' },
      { seq: 4, type: 'round', number: 2 }, { seq: 5, type: 'token', text: 'The next round is currently streaming.' },
    ], next_cursor: 5, status: 'running', finished: false }), run, [
      { id: 9, role: 'assistant', content: repeated }, { id: 10, role: 'user', content: 'Active request.' },
      { id: 11, role: 'assistant', content: repeated }, { id: 12, role: 'tool', content: 'Saved tool result.' },
    ]);
    await openApp(); await advance(450);
    expect(screen.getAllByText(repeated)).toHaveLength(2);
    expect(screen.getByText('The next round is currently streaming.')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Pause run' })).toBeTruthy();
  });

  it('preserves each legitimate identical saved round and an equal newly streaming round', async () => {
    vi.useFakeTimers();
    const repeated = 'The same answer can be legitimate in another round.';
    const run = { id: 'run', chat_id: 'chat', status: 'running', request_message_id: 10 };
    coordinator(run, () => ({ events: [
      { seq: 1, type: 'round', number: 1 }, { seq: 2, type: 'token', text: repeated },
      { seq: 3, type: 'tool', name: 'web_fetch', invocation_id: 'call-1', state: 'done' },
      { seq: 4, type: 'round', number: 2 }, { seq: 5, type: 'token', text: repeated },
      { seq: 6, type: 'tool', name: 'web_fetch', invocation_id: 'call-2', state: 'done' },
      { seq: 7, type: 'round', number: 3 }, { seq: 8, type: 'token', text: repeated },
    ], next_cursor: 8, status: 'running', finished: false }), run, [
      { id: 10, role: 'user', content: 'Active request.' }, { id: 11, role: 'assistant', content: repeated },
      { id: 12, role: 'tool', content: 'First result.' }, { id: 13, role: 'assistant', content: repeated },
      { id: 14, role: 'tool', content: 'Second result.' },
    ]);
    await openApp(); await advance(450);
    expect(screen.getAllByText(repeated)).toHaveLength(3);
    expect(screen.getByRole('button', { name: 'Pause run' })).toBeTruthy();
  });

  it('continues through a 1000-event page containing an old cancelled turn', async () => {
    vi.useFakeTimers();
    const history: RunEvent[] = Array.from({ length: 999 }, (_, index) => ({ seq: index + 1, type: 'status', text: 'Historical work' }));
    history.push({ seq: 1000, type: 'done', status: 'cancelled', cancelled: true });
    const resumed: RunEvent[] = [{ seq: 1001, type: 'token', text: 'Resumed answer is still streaming.' },
      ...Array.from({ length: 999 }, (_, index) => ({ seq: index + 1002, type: 'status', text: 'Resumed work' }))];
    const call = coordinator({ id: 'run', chat_id: 'chat', status: 'running' }, after => {
      if (after === 0) return { events: history, next_cursor: 1000, has_more: true, status: 'running', finished: false };
      if (after === 1000) return { events: resumed, next_cursor: 2000, has_more: false, status: 'running', finished: false };
      if (after === 2000) return { events: [{ seq: 2001, type: 'done' }], next_cursor: 2001, status: 'completed', finished: true };
      throw new Error(`Unexpected poll cursor ${after}`);
    });
    await openApp(); await advance(450);
    expect(screen.getByRole('button', { name: 'Pause run' })).toBeTruthy();
    await advance(450);
    expect(screen.getByText('Resumed answer is still streaming.')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Pause run' })).toBeTruthy();
    await advance(450);
    expect(call.mock.calls.filter(([action]) => action === 'poll').map(([, data]) => data?.after)).toEqual([0, 1000, 2000]);
    expect(screen.queryByRole('button', { name: 'Pause run' })).toBeNull();
    await advance(900);
    expect(call.mock.calls.filter(([action]) => action === 'poll')).toHaveLength(3);
  });

  it('rediscovers a known finished run resumed by another client without replaying its consumed cursor', async () => {
    vi.useFakeTimers();
    const run = { id: 'run', chat_id: 'chat', status: 'running' };
    const call = coordinator(run, after => after === 0
      ? { events: [{ seq: 37, type: 'done', status: 'paused' }], next_cursor: 37, status: 'paused', finished: true }
      : { events: [{ seq: 38, type: 'token', text: 'Continued from Telegram.' }], next_cursor: 38, status: 'running', finished: false }, run);
    await openApp(); await advance(450);
    expect(screen.queryByRole('button', { name: 'Pause run' })).toBeNull();
    expect(screen.getByRole('button', { name: 'Resume' })).toBeTruthy();
    await advance(3050); await advance(100);
    expect(call).toHaveBeenCalledWith('runs', {});
    expect(call.mock.calls.filter(([action]) => action === 'poll').map(([, data]) => data?.after)).toEqual([0, 37]);
    expect(screen.getByText('Continued from Telegram.')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Pause run' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Resume' })).toBeNull();
    expect(call.mock.calls.some(([action]) => ['start_chat', 'resume'].includes(action))).toBe(false);
  });
});

describe('saved transcript reconciliation', () => {
  const assistant = (content: string, id?: number): Message => ({ id, role: 'assistant', content });
  const transcript = (messages: Message[]): SavedChat => ({ id: 'chat', title: 'Fixture', project_id: null, model: 'fixture', messages });
  const active = (parts: Message[]) => ({ ...initial(), run: { id: 'run', chat_id: 'chat', request_message_id: 10 }, parts });
  const request = { id: 10, role: 'user', content: 'Active request.' };

  it('consumes each matching saved occurrence once rather than deduplicating equal text', () => {
    const live = active([assistant('Repeat.'), assistant('Repeat.'), assistant('Repeat.')]);
    const visible = reconcileRunMessages(live, transcript([request, assistant('Repeat.', 11), assistant('Repeat.', 12)]));
    expect(visible.parts).toHaveLength(1); expect(live.parts).toHaveLength(3);
  });
  it('keeps pre-request and unrelated later requests outside the matching scope', () => {
    const live = active([assistant('Repeat.')]);
    expect(reconcileRunMessages(live, transcript([assistant('Repeat.', 9), request])).parts).toHaveLength(1);
    expect(reconcileRunMessages(live, transcript([request, { id: 11, role: 'user', content: 'Another request.' }, assistant('Repeat.', 12)])).parts).toHaveLength(1);
  });
  it('stops at the first position or thinking mismatch instead of searching later text', () => {
    const live = active([assistant('First.'), assistant('Repeat.')]);
    expect(reconcileRunMessages(live, transcript([request, assistant('Different.', 11), assistant('Repeat.', 12)])).parts).toHaveLength(2);
    expect(reconcileRunMessages(active([{ ...assistant('Repeat.'), thinking: 'New reasoning.' }]), transcript([request, { ...assistant('Repeat.', 11), thinking: 'Earlier reasoning.' }])).parts).toHaveLength(1);
  });
  it('retains replay output when IDs or the visible request position cannot prove ownership', () => {
    const live = active([assistant('Repeat.')]);
    expect(reconcileRunMessages({ ...live, run: { id: 'run', chat_id: 'chat' } }, transcript([request, assistant('Repeat.', 11)])).parts).toHaveLength(1);
    expect(reconcileRunMessages(live, transcript([assistant('Repeat.', 11)])).parts).toHaveLength(1);
    expect(reconcileRunMessages(live, transcript([request, assistant('Repeat.')])).parts).toHaveLength(1);
    expect(reconcileRunMessages(live, { ...transcript([request, assistant('Repeat.', 11)]), id: 'another-chat' }).parts).toHaveLength(1);
  });
  it('hides an exactly matched historical partial only after its persisted-round event', () => {
    const chat = transcript([request, { ...assistant('Saved partial.', 11), status: 'partial' }]);
    const replay = applyEvents(active([]), [
      { seq: 1, type: 'round', number: 1 }, { seq: 2, type: 'token', text: 'Saved partial.' },
      { seq: 3, type: 'done', status: 'paused' },
    ], { status: 'running', finished: false });
    expect(reconcileRunMessages(replay, chat).showCurrent).toBe(false);
    const resumed = applyEvents(replay, [{ seq: 4, type: 'round', number: 2 }, { seq: 5, type: 'token', text: 'Saved partial.' }], { status: 'running', finished: false });
    expect(reconcileRunMessages(resumed, chat).parts).toHaveLength(0);
    expect(reconcileRunMessages(resumed, chat).showCurrent).toBe(true);
  });
  it('does not hide current text after any unmatched earlier round', () => {
    const live = { ...active([assistant('Different first round.')]), text: 'Repeat.', currentPersisted: true };
    expect(reconcileRunMessages(live, transcript([request, assistant('Saved first round.', 11), assistant('Repeat.', 12)])).showCurrent).toBe(true);
  });
});
