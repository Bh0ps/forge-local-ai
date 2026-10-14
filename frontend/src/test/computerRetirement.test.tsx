import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ComputerView, type ComputerViewState } from '../computerView';

const retained = (chatId = 'synthetic-chat'): ComputerViewState => ({
  chat_id: chatId, scope_run_id: null, mode: 'shared_desktop', run_id: 'synthetic-parent',
  invocation_id: 'synthetic-child:2', tool: 'computer_set_value', status: 'completed',
  action_count: 2, action_count_truncated: false, retired_action_count: 2,
  retired_action_count_truncated: false, evidence_mode: 'none', snapshot: null, failure: null,
});
function bridge(response: (data: {chat_id: string}) => unknown) {
  const call = vi.fn(async (action: string, data: {chat_id: string}) => {
    if (action !== 'computer_view_state') throw new Error('Unexpected synthetic action');
    return response(data);
  });
  window.pywebview = {api: {call}};
  return call;
}
async function tick(ms = 1) {await act(async () => {await vi.advanceTimersByTimeAsync(ms);});}
beforeEach(() => {vi.useFakeTimers();});
afterEach(() => {cleanup(); vi.clearAllTimers(); vi.useRealTimers(); vi.restoreAllMocks();});

describe('retired helper desktop activity', () => {
  it('preserves recorded steps through retirement polling and reopening without fetching removed pixels', async () => {
    const retired = retained();
    let response: ComputerViewState = {...retired, retired_action_count: 0, status: 'running',
      evidence_mode: 'image', snapshot: {run_id: 'synthetic-child', invocation_id: 'synthetic-child:1',
        data_url: 'data:image/png;base64,iVBORw0KGgo=', mime_type: 'image/png', width: 32, height: 24, age_seconds: 3}};
    const call = bridge(() => response);
    const mounted = render(<ComputerView chatId="synthetic-chat" onActivity={() => {}}/>);
    await tick();
    expect(screen.getByText('2 recorded steps')).toBeTruthy();
    expect(screen.getByRole('img', {name: 'Earlier shared-desktop snapshot'})).toBeTruthy();
    const activity = screen.getByRole('button', {name: 'Activity'}); activity.focus();
    response = retired;
    await tick(1500);
    expect(screen.getByText('Request completed')).toBeTruthy();
    expect(screen.getByText('2 recorded helper steps')).toBeTruthy();
    expect(screen.queryByText('Earlier helper activity · 2 steps')).toBeNull();
    expect(screen.getByText('Earlier helper activity retained. No snapshot is available.')).toBeTruthy();
    expect(screen.queryByRole('img')).toBeNull();
    expect(document.activeElement).toBe(activity);
    mounted.unmount();
    render(<ComputerView chatId="synthetic-chat" onActivity={() => {}}/>); await tick();
    expect(screen.getByText('2 recorded helper steps')).toBeTruthy();
    expect(call.mock.calls.map(([action]) => action)).toEqual(['computer_view_state', 'computer_view_state', 'computer_view_state']);
  });

  it('distinguishes surviving parent steps from earlier helper steps and keeps keyboard Activity available', async () => {
    vi.useRealTimers();
    bridge(() => ({...retained(), action_count: 3, tool: 'computer_windows'}));
    const onActivity = vi.fn();
    render(<ComputerView chatId="synthetic-chat" onActivity={onActivity}/>);
    await screen.findByText('3 recorded steps');
    expect(screen.getByText('Earlier helper activity · 2 steps')).toBeTruthy();
    expect(screen.getByText('Listed apps')).toBeTruthy();
    const button = screen.getByRole('button', {name: 'Activity'}); button.focus();
    await userEvent.setup().keyboard('{Enter}');
    expect(onActivity).toHaveBeenCalledTimes(1);
    expect(document.activeElement).toBe(button);
  });

  it('labels bounded retained counts as lower bounds', async () => {
    bridge(() => ({...retained(), action_count: 2048, action_count_truncated: true,
      retired_action_count: 2048, retired_action_count_truncated: true}));
    render(<ComputerView chatId="synthetic-chat" onActivity={() => {}}/>); await tick();
    expect(screen.getByText('At least 2048 recorded steps')).toBeTruthy();
    expect(screen.getByText('Earlier helper activity · at least 2048 steps')).toBeTruthy();
  });

  it('does not mistake capped equal counts for exclusively retired helper activity', async () => {
    bridge(() => ({...retained(), action_count: 2048, action_count_truncated: true,
      retired_action_count: 2048, retired_action_count_truncated: false}));
    render(<ComputerView chatId="synthetic-chat" onActivity={() => {}}/>); await tick();
    expect(screen.getByText('At least 2048 recorded steps')).toBeTruthy();
    expect(screen.getByText('Earlier helper activity · 2048 steps')).toBeTruthy();
    expect(screen.queryByText('2048 recorded helper steps')).toBeNull();
  });

  it('keeps exact singular helper provenance compact', async () => {
    bridge(() => ({...retained(), action_count: 1, retired_action_count: 1}));
    render(<ComputerView chatId="synthetic-chat" onActivity={() => {}}/>); await tick();
    expect(screen.getByText('1 recorded helper step')).toBeTruthy();
    expect(screen.queryByText('Earlier helper activity · 1 step')).toBeNull();
  });

  it('does not invent retained helper activity for legacy responses without that metadata', async () => {
    const legacy = retained(); delete legacy.retired_action_count; delete legacy.retired_action_count_truncated;
    bridge(() => ({...legacy, action_count: 0, tool: null, invocation_id: null}));
    render(<ComputerView chatId="synthetic-chat" onActivity={() => {}}/>); await tick();
    expect(screen.getByText('No desktop snapshot yet.')).toBeTruthy();
    expect(screen.queryByText(/Earlier helper activity/)).toBeNull();
    expect(screen.queryByRole('button', {name: 'Activity'})).toBeNull();
  });

  it('ignores a delayed retired response from the previous chat and reuses only the current owner', async () => {
    let settle!: (value: ComputerViewState) => void;
    const old = new Promise<ComputerViewState>(resolve => {settle = resolve;});
    const current = {...retained('new-synthetic-chat'), action_count: 0, retired_action_count: 0, status: 'queued'};
    bridge(data => data.chat_id === 'synthetic-chat' ? old : current);
    const mounted = render(<ComputerView chatId="synthetic-chat" onActivity={() => {}}/>); await tick();
    mounted.rerender(<ComputerView chatId="new-synthetic-chat" onActivity={() => {}}/>); await tick();
    await act(async () => {settle(retained());});
    expect(screen.getByText('Queued')).toBeTruthy();
    expect(screen.queryByText(/Earlier helper activity/)).toBeNull();
    fireEvent.click(screen.getByRole('button', {name: 'Refresh desktop activity'})); await tick();
    expect(screen.queryByText(/Earlier helper activity/)).toBeNull();
  });

  it.each([true, -1, 2049, 3, '2'])('does not display malformed or impossible optional retained count %s', async count => {
    bridge(() => ({...retained(), retired_action_count: count}));
    render(<ComputerView chatId="synthetic-chat" onActivity={() => {}}/>); await tick();
    expect(screen.queryByText(/Earlier helper activity/)).toBeNull();
    expect(screen.getByText('2 recorded steps')).toBeTruthy();
  });

  it.each([1, undefined])('does not invent exactness from a missing or nonboolean truncation flag %s', async truncated => {
    bridge(() => ({...retained(), retired_action_count_truncated: truncated}));
    render(<ComputerView chatId="synthetic-chat" onActivity={() => {}}/>); await tick();
    expect(screen.queryByText(/Earlier helper activity/)).toBeNull();
  });
});
