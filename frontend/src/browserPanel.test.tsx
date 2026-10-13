import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { BrowserPanel } from './browserPanel';
import { updateReadiness } from './updateReadiness';

const initial = { running: true, visible: true, available: true, url: 'https://initial.invalid/', title: 'Initial page' };
function deferred<T = unknown>() {
  let resolve!: (value: T) => void; let reject!: (reason: Error) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function bridge(overrides: Record<string, unknown> = {}) {
  const responses: Record<string, unknown> = { browser_native_status: initial, browser_native_show: { visible: true }, browser_native_hide: { visible: false }, ...overrides };
  const call = vi.fn(async (action: string, data: Record<string, unknown> = {}) => {
    if (!(action in responses)) throw new Error(`Unexpected action ${action}`);
    const response = responses[action]; return typeof response === 'function' ? response(data) : response;
  });
  window.pywebview = { api: { call, mode: vi.fn(async () => ({ ok: true })) } };
  return { call, responses };
}
async function mount() { const view = render(<BrowserPanel />); await act(async () => {}); return view; }
function address() { return screen.getByRole('textbox', { name: 'Agent browser address' }) as HTMLInputElement; }
function submit(value: string) {
  fireEvent.focus(address()); fireEvent.change(address(), { target: { value } }); fireEvent.submit(address().closest('form')!);
}
async function poll() { await act(async () => { await vi.advanceTimersByTimeAsync(1500); }); }

beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
  vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => window.setTimeout(() => callback(performance.now()), 1));
  vi.stubGlobal('cancelAnimationFrame', (id: number) => window.clearTimeout(id));
  const rect = { x: 900, y: 90, width: 320, height: 600, top: 90, left: 900, right: 1220, bottom: 690, toJSON: () => ({}) };
  vi.spyOn(Element.prototype, 'getBoundingClientRect').mockReturnValue(rect as DOMRect);
});
afterEach(() => { cleanup(); updateReadiness.aborted('browser-fixture'); vi.clearAllTimers(); vi.useRealTimers(); vi.unstubAllGlobals(); });

describe('browser address request ownership', () => {
  it('holds a submitted URL through blur and an older poll until navigation settles', async () => {
    const navigation = deferred(); const oldPoll = deferred(); const { responses, call } = bridge({ browser_native_navigate: navigation.promise });
    await mount(); expect(address().value).toBe(initial.url);
    responses.browser_native_status = oldPoll.promise; await poll();
    submit('next.invalid/research'); fireEvent.blur(address());
    expect(address().value).toBe('https://next.invalid/research');
    await act(async () => { oldPoll.resolve({ ...initial, title: 'Stale poll page' }); });
    expect(address().value).toBe('https://next.invalid/research'); expect(screen.queryByText(/Stale poll page/)).toBeNull();
    const polls = call.mock.calls.filter(([name]) => name === 'browser_native_status').length;
    await poll(); expect(call.mock.calls.filter(([name]) => name === 'browser_native_status')).toHaveLength(polls);
    await act(async () => { navigation.resolve({ ...initial, url: 'https://next.invalid/canonical', title: 'New page' }); });
    expect(address().value).toBe('https://next.invalid/canonical'); expect(screen.getByText(/New page/)).toBeTruthy();
  });

  it('keeps failed input and its error through successful binds and polling, then permits retry', async () => {
    const navigation = deferred(); const binding = deferred(); const { responses } = bridge({ browser_native_navigate: navigation.promise, browser_native_show: binding.promise });
    await mount(); await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    submit('retry.invalid/path'); fireEvent.blur(address());
    await act(async () => { navigation.reject(new Error('Synthetic navigation failure.')); });
    expect(address().value).toBe('https://retry.invalid/path'); expect(screen.getByText('Synthetic navigation failure.')).toBeTruthy();
    await act(async () => { binding.resolve({ visible: true }); }); await poll();
    expect(address().value).toBe('https://retry.invalid/path'); expect(screen.getByText('Synthetic navigation failure.')).toBeTruthy();
    responses.browser_native_navigate = { ...initial, url: 'https://retry.invalid/path', title: 'Retried page' };
    fireEvent.submit(address().closest('form')!); await act(async () => {});
    expect(screen.queryByText('Synthetic navigation failure.')).toBeNull(); expect(address().value).toBe('https://retry.invalid/path');
  });

  it('preserves a newer unsent edit after blur, late navigation, and later native status', async () => {
    const navigation = deferred(); const { responses } = bridge({ browser_native_navigate: navigation.promise });
    await mount(); submit('submitted.invalid');
    fireEvent.change(address(), { target: { value: 'later.invalid/unsent' } }); fireEvent.blur(address());
    await act(async () => { navigation.resolve({ ...initial, url: 'https://submitted.invalid/', title: 'Submitted page' }); });
    responses.browser_native_status = { ...initial, url: 'https://agent.invalid/', title: 'Agent page' }; await poll();
    expect(address().value).toBe('later.invalid/unsent'); expect(screen.getByText(/Agent page/)).toBeTruthy();
    responses.browser_native_navigate = { ...initial, url: 'https://later.invalid/unsent' };
    fireEvent.submit(address().closest('form')!); await act(async () => {});
    expect(address().value).toBe('https://later.invalid/unsent');
  });

  it.each(['resolve', 'reject'] as const)('ignores an older action that settles by %s after a newer navigation', async outcome => {
    const first = deferred(); const second = deferred(); let count = 0;
    const { responses } = bridge({ browser_native_navigate: () => ++count === 1 ? first.promise : second.promise });
    await mount(); submit('first.invalid'); submit('second.invalid'); fireEvent.blur(address());
    await act(async () => { second.resolve({ ...initial, url: 'https://second.invalid/', title: 'Second page' }); });
    await act(async () => {
      if (outcome === 'resolve') first.resolve({ ...initial, url: 'https://first.invalid/', title: 'Stale action page' });
      else first.reject(new Error('Stale action failure.'));
    });
    expect(address().value).toBe('https://second.invalid/'); expect(screen.getByText(/Second page/)).toBeTruthy();
    expect(screen.queryByText(/Stale action/)).toBeNull();
    responses.browser_native_status = { ...initial, url: 'https://new-status.invalid/' }; await poll();
    expect(address().value).toBe('https://new-status.invalid/');
  });

  it.each(['resolve', 'reject'] as const)('ignores an older poll that settles by %s after a newer poll', async outcome => {
    const first = deferred(); const second = deferred(); let count = 0;
    bridge({ browser_native_status: () => ++count === 1 ? first.promise : second.promise });
    await mount(); await poll();
    await act(async () => { second.resolve({ ...initial, url: 'https://current.invalid/', title: 'Current page' }); });
    await act(async () => {
      if (outcome === 'resolve') first.resolve({ ...initial, title: 'Old poll page' });
      else first.reject(new Error('Old poll failure.'));
    });
    expect(address().value).toBe('https://current.invalid/'); expect(screen.getByText(/Current page/)).toBeTruthy();
    expect(screen.queryByText(/Old poll/)).toBeNull();
  });

  it('lets a newer history action own the address without waiting for an older navigation', async () => {
    const navigation = deferred();
    const { responses, call } = bridge({ browser_native_navigate: navigation.promise, browser_native_back: { ...initial, url: 'https://previous.invalid/', title: 'Previous page' } });
    await mount(); submit('slow.invalid'); fireEvent.blur(address());
    fireEvent.click(screen.getByRole('button', { name: 'Browser back' })); await act(async () => {});
    expect(address().value).toBe('https://previous.invalid/');
    responses.browser_native_status = { ...initial, url: 'https://history.invalid/', title: 'History page' }; await poll();
    expect(call.mock.calls.filter(([name]) => name === 'browser_native_status')).toHaveLength(2);
    await act(async () => { navigation.resolve({ ...initial, url: 'https://slow.invalid/', title: 'Superseded navigation' }); });
    expect(address().value).toBe('https://history.invalid/'); expect(screen.queryByText(/Superseded navigation/)).toBeNull();
  });

  it('keeps native status current while focused and reveals its address only after an unchanged blur', async () => {
    const { responses } = bridge(); await mount(); fireEvent.focus(address());
    responses.browser_native_status = { ...initial, url: 'https://agent.invalid/', title: 'Agent page' }; await poll();
    expect(address().value).toBe(initial.url); expect(screen.getByText(/Agent page/)).toBeTruthy();
    fireEvent.blur(address()); expect(address().value).toBe('https://agent.invalid/');
  });

  it('does not carry delayed action or poll replies into a reopened browser panel', async () => {
    const navigation = deferred(); const oldPoll = deferred(); const { responses } = bridge({ browser_native_navigate: navigation.promise });
    const previous = await mount(); responses.browser_native_status = oldPoll.promise; await poll(); submit('previous.invalid'); previous.unmount();
    responses.browser_native_status = { ...initial, url: 'https://reopened.invalid/', title: 'Reopened page' }; await mount();
    await act(async () => { navigation.reject(new Error('Previous panel failure.')); oldPoll.resolve({ ...initial, title: 'Previous panel poll' }); });
    expect(address().value).toBe('https://reopened.invalid/'); expect(screen.getByText(/Reopened page/)).toBeTruthy();
    expect(screen.queryByText(/Previous panel/)).toBeNull();
  });

  it('disables browsing during restart readiness and rejects synthetic mutation events until acknowledged cancellation', async () => {
    const { call } = bridge({ browser_native_navigate: { ...initial, url: 'https://after.invalid/' } }); await mount();
    await act(async () => { expect(updateReadiness.start('browser-fixture', vi.fn())).toBe(true); });
    expect(address().disabled).toBe(true);
    for (const name of ['Browser back', 'Browser forward', 'Reload browser']) expect((screen.getByRole('button', { name }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(address(), { target: { value: 'blocked.invalid' } }); fireEvent.submit(address().closest('form')!);
    expect(address().value).toBe(initial.url); expect(call.mock.calls.some(([name]) => name === 'browser_native_navigate')).toBe(false);
    await act(async () => { updateReadiness.aborted('browser-fixture'); }); submit('after.invalid'); await act(async () => {});
    expect(call).toHaveBeenCalledWith('browser_native_navigate', { url: 'https://after.invalid' });
  });
});
