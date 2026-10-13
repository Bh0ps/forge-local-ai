import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { UpdateSettings } from './updateSettings';

const current = { current_version: '5.1.1', state: 'current', automatic_install_available: false,
  signing_setup_required: true, message: 'Unsigned fixture preview.', checked_at: 1_000_000 };

function bridge(status: unknown, checking: unknown = { ...current, state: 'checking' }) {
  const call = vi.fn(async (action: string) => {
    if (action === 'host_status') return { visible: true, visibility_seq: 0 };
    if (action === 'update_status') return typeof status === 'function' ? status() : status;
    if (action === 'update_check') return checking;
    if (action === 'update_cancel') return { ok: true };
    throw new Error('Unexpected synthetic action: ' + action);
  });
  window.pywebview = { api: { call } };
  return call;
}
const statusCalls = (call: ReturnType<typeof bridge>) => call.mock.calls.filter(([action]) => action === 'update_status').length;

afterEach(() => { vi.useRealTimers(); });

describe('update settings', () => {
  it('shows preference defaults without saving and only changes explicit controls', async () => {
    bridge(current); const change = vi.fn(async () => {});
    render(<UpdateSettings notify={vi.fn()} onChange={change} />);
    await screen.findByText('Up to date');
    expect(change).not.toHaveBeenCalled();
    expect(screen.getByRole('switch', { name: 'Check for updates automatically' }).getAttribute('aria-checked')).toBe('true');
    expect((screen.getByRole('combobox', { name: 'Update channel' }) as HTMLSelectElement).value).toBe('preview');
    fireEvent.click(screen.getByRole('switch', { name: 'Check for updates automatically' }));
    await waitFor(() => expect(change).toHaveBeenCalledWith({ update_checks: false }));
    fireEvent.change(screen.getByRole('combobox', { name: 'Update channel' }), { target: { value: 'stable' } });
    await waitFor(() => expect(change).toHaveBeenCalledWith({ update_channel: 'stable' }));
  });

  it('starts a non-blocking check on the selected channel and prevents duplicate clicks', async () => {
    const call = bridge(current);
    render(<UpdateSettings notify={vi.fn()} settings={{ update_channel: 'stable' }} onChange={vi.fn(async () => {})} />);
    await screen.findByText('Up to date');
    fireEvent.click(screen.getByRole('button', { name: 'Check for updates' }));
    await waitFor(() => expect(call).toHaveBeenCalledWith('update_check', { channel: 'stable', background: true }));
    expect(await screen.findByText('Checking…')).toBeTruthy();
    expect((screen.getByRole('button', { name: 'Check for updates' }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeTruthy();
  });

  it('offers a manual public download for an unsigned preview without an install action', async () => {
    bridge({ ...current, state: 'available', verified: true, release: { version: '5.1.2', prerelease: true, size: 1200,
      html_url: 'https://github.com/Bh0ps/forge-local-ai/releases/tag/v5.1.2',
      url: 'https://github.com/Bh0ps/forge-local-ai/releases/download/v5.1.2/Forge-5.1.2-Setup.exe' } });
    render(<UpdateSettings notify={vi.fn()} />);
    const download = await screen.findByRole('link', { name: 'Download installer' });
    expect(download.getAttribute('href')).toContain('/releases/download/v5.1.2/Forge-5.1.2-Setup.exe');
    expect(screen.getByText('Forge 5.1.2 preview is available.')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Install & restart' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Download & verify' })).toBeNull();
  });

  it('does not render a foreign installer link from malformed status', async () => {
    bridge({ ...current, state: 'available', release: { version: '5.1.2', size: 1200,
      html_url: 'https://github.com/Bh0ps/forge-local-ai/releases/tag/v5.1.2', url: 'https://example.invalid/installer.exe' } });
    render(<UpdateSettings notify={vi.fn()} />);
    expect((await screen.findByRole('link', { name: 'Download installer' })).getAttribute('href')).toBe('https://github.com/Bh0ps/forge-local-ai/releases/tag/v5.1.2');
  });

  it('does not keep polling an idle page', async () => {
    vi.useFakeTimers(); const call = bridge(current);
    render(<UpdateSettings notify={vi.fn()} />);
    await act(async () => { await Promise.resolve(); });
    expect(statusCalls(call)).toBe(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
    expect(statusCalls(call)).toBe(1);
  });

  it('polls a running check only while visible and stops after its honest failure state', async () => {
    vi.useFakeTimers(); let status = { ...current, state: 'checking', operation_error: '' };
    const call = bridge(() => status);
    render(<UpdateSettings notify={vi.fn()} />);
    await act(async () => { await Promise.resolve(); });
    await act(async () => { await vi.advanceTimersByTimeAsync(1500); });
    expect(statusCalls(call)).toBe(2);
    const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden');
    fireEvent(document, new Event('visibilitychange'));
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
    expect(statusCalls(call)).toBe(2);
    status = { ...status, state: 'error', operation_error: 'Synthetic connection unavailable.' };
    visibility.mockReturnValue('visible'); fireEvent(document, new Event('visibilitychange'));
    await act(async () => { await Promise.resolve(); });
    expect(screen.getByText('Check failed')).toBeTruthy();
    expect(screen.getByRole('alert').textContent).toContain('Synthetic connection unavailable.');
    const completedCalls = statusCalls(call);
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
    expect(statusCalls(call)).toBe(completedCalls);
  });

  it('also suspends polling when the native window is hidden', async () => {
    vi.useFakeTimers(); const call = bridge({ ...current, state: 'checking' });
    render(<UpdateSettings notify={vi.fn()} />);
    await act(async () => { await Promise.resolve(); });
    fireEvent(window, new CustomEvent('forge:visibility', { detail: { visible: false, seq: 1 } }));
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
    expect(statusCalls(call)).toBe(1);
    fireEvent(window, new CustomEvent('forge:visibility', { detail: { visible: true, seq: 2 } }));
    await act(async () => { await Promise.resolve(); await vi.advanceTimersByTimeAsync(1500); });
    expect(statusCalls(call)).toBe(3);
  });

  it('ignores a delayed initial response after a newer manual check starts', async () => {
    let resolve!: (value: unknown) => void;
    const pending = new Promise(done => { resolve = done; });
    const call = bridge(() => pending);
    render(<UpdateSettings notify={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: 'Check for updates' }));
    await waitFor(() => expect(call).toHaveBeenCalledWith('update_check', { channel: 'preview', background: true }));
    expect(await screen.findByText('Checking…')).toBeTruthy();
    await act(async () => { resolve(current); await pending; });
    expect(screen.getByText('Checking…')).toBeTruthy();
    expect(screen.queryByText('Up to date')).toBeNull();
  });
});
