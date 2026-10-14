import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { BrowserPanel } from './browserPanel';
import { ComputerView, StopComputerActivity, type ComputerViewState } from './computerView';
import { updateReadiness } from './updateReadiness';
import userEvent from '@testing-library/user-event';

function deferred<T = unknown>() {let resolve!: (value: T) => void, reject!: (value: Error) => void; const promise = new Promise<T>((yes, no) => {resolve = yes; reject = no;}); return {promise, resolve, reject};}
const evidence = (chat_id = 'first', scope_run_id: string | null = null): ComputerViewState => ({chat_id, scope_run_id, mode: 'shared_desktop', run_id: 'own-run', invocation_id: 'own-run:1', tool: 'computer_set_value', status: 'running', action_count: 2, action_count_truncated: false, evidence_mode: 'image', snapshot: {run_id: 'own-run', invocation_id: 'own-run:0', data_url: 'data:image/png;base64,iVBORw0KGgo=', mime_type: 'image/png', width: 32, height: 24, age_seconds: 8}, failure: null});
function bridge(overrides: Record<string, unknown> = {}) {
  const responses: Record<string, unknown> = {browser_native_status: {running: true, visible: true}, browser_native_show: {ok: true}, browser_native_hide: {ok: true}, computer_view_state: evidence(), emergency_stop: {ok: true}, ...overrides};
  const call = vi.fn(async (name: string, data: Record<string, unknown>) => {if (!(name in responses)) throw new Error('Unexpected synthetic action'); const value = responses[name]; return typeof value === 'function' ? value(data) : value;});
  window.pywebview = {api: {call}}; return {call, responses};
}
async function tick(ms = 1) {await act(async () => {await vi.advanceTimersByTimeAsync(ms);});}
beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal('ResizeObserver', class {observe() {} disconnect() {}});
  vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => window.setTimeout(() => callback(performance.now()), 1));
  vi.stubGlobal('cancelAnimationFrame', (id: number) => window.clearTimeout(id));
  vi.spyOn(Element.prototype, 'getBoundingClientRect').mockReturnValue({x: 900, y: 90, width: 320, height: 600} as DOMRect);
});
afterEach(() => {cleanup(); updateReadiness.aborted('computer-fixture'); vi.clearAllTimers(); vi.useRealTimers(); vi.unstubAllGlobals();});

describe('desktop evidence view ownership and native surface release', () => {
  it('waits for pending Show and a final acknowledged Hide before reading desktop evidence', async () => {
    const shown = deferred(), hidden = deferred(); let hides = 0;
    const {call} = bridge({browser_native_show: shown.promise, browser_native_hide: () => ++hides === 1 ? {ok: true} : hidden.promise});
    render(<BrowserPanel chatId="first"/>); await tick();
    fireEvent.click(screen.getByRole('button', {name: 'Desktop'})); await tick();
    expect(screen.getByText('Hiding the browser…')).toBeTruthy();
    expect(call.mock.calls.filter(([name]) => name === 'browser_native_hide')).toHaveLength(1);
    expect(call.mock.calls.filter(([name]) => name === 'computer_view_state')).toHaveLength(0);
    await act(async () => {shown.resolve({ok: true});});
    expect(call.mock.calls.filter(([name]) => name === 'browser_native_hide')).toHaveLength(2);
    expect(screen.queryByRole('region', {name: 'Desktop activity'})).toBeNull();
    await act(async () => {hidden.resolve({ok: true});});
    expect(screen.getByRole('region', {name: 'Desktop activity'})).toBeTruthy();
    expect(call.mock.calls.map(([name]) => name)).toEqual(['browser_native_status', 'browser_native_show', 'browser_native_hide', 'browser_native_hide', 'computer_view_state']);
    expect(screen.getByText(/Earlier snapshot/)).toBeTruthy();
    expect(screen.getByText('Field value update')).toBeTruthy();
  });

  it('also drains an admitted navigation before final Hide and never exposes a failed hide as success', async () => {
    const navigation = deferred(); const {call, responses} = bridge({browser_native_navigate: navigation.promise, browser_native_hide: () => Promise.reject(new Error('synthetic-private-hide-error'))});
    render(<BrowserPanel chatId="first"/>); await tick();
    const address = screen.getByRole('textbox', {name: 'Agent browser address'});
    fireEvent.change(address, {target: {value: 'synthetic.invalid'}}); fireEvent.submit(address.closest('form')!);
    fireEvent.click(screen.getByRole('button', {name: 'Desktop'})); await tick();
    expect(call.mock.calls.filter(([name]) => name === 'browser_native_hide')).toHaveLength(1);
    await act(async () => {navigation.resolve({running: true});});
    expect(screen.getByText(/browser could not be hidden/)).toBeTruthy();
    expect(screen.queryByText('synthetic-private-hide-error')).toBeNull();
    expect(call.mock.calls.filter(([name]) => name === 'computer_view_state')).toHaveLength(0);
    responses.browser_native_hide = {ok: true}; fireEvent.click(screen.getByRole('button', {name: 'Retry switching'})); await tick();
    expect(call.mock.calls.filter(([name]) => name === 'computer_view_state')).toHaveLength(1);
  });

  it.each([{}, {ok: false}])('requires a positive native hide acknowledgment before displaying pixels (%j)', async body => {
    const {call} = bridge({browser_native_hide: body}); render(<BrowserPanel chatId="first"/>); await tick();
    fireEvent.click(screen.getByRole('button', {name: 'Desktop'})); await tick();
    expect(screen.getByText(/browser could not be hidden/)).toBeTruthy();
    expect(call.mock.calls.filter(([name]) => name === 'computer_view_state')).toHaveLength(0);
    expect(screen.queryByRole('img')).toBeNull();
  });

  it('supports keyboard selection and returns to the real browser without extra desktop actions', async () => {
    vi.useRealTimers();
    const {call} = bridge(); const user = userEvent.setup();
    render(<BrowserPanel chatId="first"/>); await act(async () => {});
    const desktop = screen.getByRole('button', {name: 'Desktop'}); desktop.focus(); await user.keyboard('{Enter}');
    await screen.findByRole('region', {name: 'Desktop activity'});
    expect(desktop.getAttribute('aria-pressed')).toBe('true');
    const browser = screen.getByRole('button', {name: 'Browser'}); browser.focus(); await user.keyboard('{Enter}');
    await screen.findByRole('textbox', {name: 'Agent browser address'});
    expect(screen.getByRole('textbox', {name: 'Agent browser address'})).toBeTruthy(); expect(document.activeElement).toBe(browser);
    expect(call.mock.calls.every(([name]) => ['browser_native_status', 'browser_native_show', 'browser_native_hide', 'computer_view_state'].includes(name))).toBe(true);
  });

  it.each(['resolve', 'reject'] as const)('ignores a delayed previous chat response settling by %s', async outcome => {
    const old = deferred(); const {call} = bridge({computer_view_state: ({chat_id}: {chat_id: string}) => chat_id === 'first' ? old.promise : evidence('second')});
    const mounted = render(<ComputerView chatId="first" onActivity={() => {}}/>); await tick();
    mounted.rerender(<ComputerView chatId="second" onActivity={() => {}}/>); await tick();
    await act(async () => {if (outcome === 'resolve') old.resolve({...evidence(), status: 'failed'}); else old.reject(new Error('synthetic-private-old-error'));});
    expect(screen.getByText('Working')).toBeTruthy(); expect(screen.queryByText(/unavailable/)).toBeNull();
    expect(call.mock.calls.filter(([name]) => name === 'computer_view_state')).toHaveLength(2);
  });

  it('refuses mismatched scope and retains no image during a transport replacement', async () => {
    bridge({computer_view_state: evidence('wrong')}); const mounted = render(<ComputerView chatId="first" onActivity={() => {}}/>); await tick();
    expect(screen.queryByRole('img')).toBeNull(); expect(screen.getByText(/Desktop activity is unavailable/)).toBeTruthy();
    const current = deferred(); bridge({computer_view_state: current.promise});
    mounted.rerender(<ComputerView chatId="first" onActivity={() => {}}/>); await tick();
    expect(screen.queryByRole('img')).toBeNull();
    await act(async () => {current.resolve(evidence());});
    expect(screen.getByRole('img', {name: 'Earlier shared-desktop snapshot'})).toBeTruthy();
  });

  it('suspends hidden polling and admits at most one outstanding evidence read', async () => {
    let hidden = true; vi.spyOn(document, 'visibilityState', 'get').mockImplementation(() => hidden ? 'hidden' : 'visible');
    const pending = deferred(); const {call, responses} = bridge({computer_view_state: pending.promise});
    render(<ComputerView chatId="first" onActivity={() => {}}/>); await tick(4500);
    expect(call).not.toHaveBeenCalled(); hidden = false; fireEvent(document, new Event('visibilitychange')); await tick(4500);
    expect(call).toHaveBeenCalledTimes(1);
    responses.computer_view_state = evidence(); await act(async () => {pending.resolve(evidence());}); await tick(1500);
    expect(call).toHaveBeenCalledTimes(2); hidden = true; await tick(3000); expect(call).toHaveBeenCalledTimes(2);
  });

  it('has no capture or control effect without evidence, and opens the existing Activity view', async () => {
    const onActivity = vi.fn(); const {call} = bridge({computer_view_state: {...evidence(), evidence_mode: 'accessibility_only', snapshot: null}});
    render(<ComputerView chatId="first" onActivity={onActivity}/>); await tick();
    expect(screen.getByText('Accessibility evidence recorded. No image is available.')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', {name: 'Activity'})); expect(onActivity).toHaveBeenCalledTimes(1);
    expect(call.mock.calls.map(([name]) => name)).toEqual(['computer_view_state']);
  });

  it('suspends native-host hidden reads even when WebView document visibility remains visible', async () => {
    const {call} = bridge(); const mounted = render(<ComputerView chatId="first" visible={false} onActivity={() => {}}/>); await tick(4500);
    expect(call).not.toHaveBeenCalled();
    mounted.rerender(<ComputerView chatId="first" visible onActivity={() => {}}/>); await tick();
    expect(call).toHaveBeenCalledTimes(1);
    mounted.rerender(<ComputerView chatId="first" visible={false} onActivity={() => {}}/>); await tick(4500);
    expect(call).toHaveBeenCalledTimes(1);
  });

  it('keeps stop available through readiness and reports only stop requested', async () => {
    const {call} = bridge(); render(<StopComputerActivity/>);
    expect(updateReadiness.start('computer-fixture', vi.fn())).toBe(true);
    fireEvent.click(screen.getByRole('button', {name: 'Stop all agents'})); await tick();
    expect(call).toHaveBeenCalledWith('emergency_stop', {});
    expect(screen.getByText('Stop requested. An action already sent may finish.')).toBeTruthy();
    expect(updateReadiness.canEdit()).toBe(false);
  });

  it('sanitizes an unconfirmed stop instead of claiming success', async () => {
    bridge({emergency_stop: {error: 'synthetic-private-stop-body'}}); render(<StopComputerActivity/>);
    fireEvent.click(screen.getByRole('button', {name: 'Stop all agents'})); await tick();
    expect(screen.getByText(/Stop could not be confirmed/)).toBeTruthy(); expect(screen.queryByText(/synthetic-private/)).toBeNull();
    expect(screen.queryByText(/Stop requested/)).toBeNull();
  });
});

describe('recorded desktop operations without an outcome claim', () => {
  it('shows a refused-click projection as an operation and opens Activity by keyboard', async () => {
    vi.useRealTimers();
    const projection: ComputerViewState = {...evidence('first', 'own-run'), tool: 'computer_click', status: 'queued',
      action_count: 1, evidence_mode: 'none', snapshot: null};
    const {call} = bridge({computer_view_state: projection});
    const onActivity = vi.fn(); render(<ComputerView chatId="first" runId="own-run" onActivity={onActivity}/>);
    await screen.findByText('Target click');
    expect(screen.getByText('Queued')).toBeTruthy(); expect(screen.getByText('1 recorded step')).toBeTruthy();
    expect(screen.queryByText('Clicked target')).toBeNull(); expect(screen.queryByRole('img')).toBeNull();
    const activity = screen.getByRole('button', {name: 'Activity'}); activity.focus();
    await userEvent.setup().keyboard('{Enter}');
    expect(onActivity).toHaveBeenCalledOnce(); expect(document.activeElement).toBe(activity);
    expect(call.mock.calls.map(([name]) => name)).toEqual(['computer_view_state']);
  });

  it('keeps completed request state separate from a refused text operation', async () => {
    const {call} = bridge({computer_view_state: {...evidence(), tool: 'computer_type', status: 'completed',
      action_count: 1, evidence_mode: 'none', snapshot: null}});
    render(<ComputerView chatId="first" onActivity={() => {}}/>); await tick();
    expect(screen.getByText('Request completed')).toBeTruthy(); expect(screen.getByText('Text entry')).toBeTruthy();
    expect(screen.getByText('1 recorded step')).toBeTruthy(); expect(screen.queryByText('Entered text')).toBeNull();
    expect(screen.queryByText(/Text entry (?:succeeded|completed|performed)/)).toBeNull();
    expect(call.mock.calls.map(([name]) => name)).toEqual(['computer_view_state']);
  });

  it('does not publish an old operation after changing runs within the same chat', async () => {
    const pending = deferred<ComputerViewState>();
    const current: ComputerViewState = {...evidence('first', 'current-run'), tool: 'computer_type',
      run_id: 'current-run', invocation_id: 'current-run:0', action_count: 1, evidence_mode: 'none', snapshot: null};
    const {call} = bridge({computer_view_state: (data: Record<string, unknown>) => data.run_id === 'old-run' ? pending.promise : current});
    const view = render(<ComputerView chatId="first" runId="old-run" onActivity={() => {}}/>); await tick();
    view.rerender(<ComputerView chatId="first" runId="current-run" onActivity={() => {}}/>); await tick();
    await act(async () => pending.resolve({...evidence('first', 'old-run'), tool: 'computer_click', status: 'completed'}));
    expect(screen.getByText('Text entry')).toBeTruthy(); expect(screen.getByText('Working')).toBeTruthy();
    expect(screen.queryByText('Target click')).toBeNull(); expect(screen.queryByText('Request completed')).toBeNull();
    expect(call.mock.calls.filter(([name]) => name === 'computer_view_state').map(([,data]) => data)).toEqual([{chat_id:'first',run_id:'old-run'},{chat_id:'first',run_id:'current-run'}]);
  });
});
