import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { SetupWizard } from './setupWizard';
import { DEFAULT_SETTINGS } from './types';

const settings = { ...DEFAULT_SETTINGS, model: 'saved:9b', context: 65536, provider_id: 'ollama' };
const models = [{ name: 'saved:9b', size: 5e9 }];
function bridge(overrides: Record<string, unknown> = {}) {
  const responses: Record<string, unknown> = {
    setup_status: { completed: false, first_run: true, state: {}, jobs: [], catalog: [] },
    providers: { providers: [{ id: 'ollama', name: 'Existing Ollama' }] },
    ...overrides,
  };
  const call = vi.fn(async (action: string, data: Record<string, unknown> = {}) => {
    if (!(action in responses)) throw new Error(`Unexpected action ${action}`);
    const result = responses[action]; return typeof result === 'function' ? result(data) : result;
  });
  window.pywebview = { api: { call } };
  return { call, responses };
}
function wizard() {
  const change = vi.fn(async () => {}); const close = vi.fn(); const refresh = vi.fn(async () => {});
  const rendered = render(<SetupWizard settings={settings} models={models} onChange={change} refreshModels={refresh} close={close} openSettings={vi.fn()} />);
  return { change, close, refresh, ...rendered };
}

describe('setup selection and durable recovery', () => {
  it('requires a click to adopt an installed managed engine and keeps saved context', async () => {
    bridge({ setup_status: { completed: false, first_run: true, state: { install_engine: { provider_id: 'runtime-fixture', provider: { id: 'runtime-fixture', name: 'Managed Ollama' }, requires_selection: true } }, jobs: [], catalog: [] } });
    const { change, refresh } = wizard();
    await userEvent.click(await screen.findByRole('button', { name: '2Engine & model' }));
    await screen.findByRole('button', { name: 'Use installed engine' });
    expect(change).not.toHaveBeenCalled();
    expect((screen.getByLabelText('Installed model') as HTMLSelectElement).value).toBe('saved:9b');
    expect(screen.getByText(/64K context/)).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: 'Use installed engine' }));
    expect(change).toHaveBeenCalledExactlyOnceWith({ provider_id: 'runtime-fixture', model: '' });
    expect(refresh).toHaveBeenCalledOnce();
  });

  it('shows interrupted job repair and retries its durable request by id', async () => {
    const { call } = bridge({ setup_status: { completed: false, first_run: true, resume_available: true, state: {}, catalog: [], jobs: [{ id: 'interrupted_fixture', operation: 'download_model', status: 'interrupted', error: 'Engine stopped; resume the download.' }] }, setup_retry: { id: 'retry_fixture' } });
    const { change } = wizard();
    expect(await screen.findByRole('alert')).toHaveProperty('textContent', 'download model: Engine stopped; resume the download.');
    await userEvent.click(screen.getByRole('button', { name: 'Retry download model' }));
    expect(call).toHaveBeenCalledWith('setup_retry', { id: 'interrupted_fixture' });
    expect(change).not.toHaveBeenCalled();
  });

  it('offers a completed download and recommendation without selecting either automatically', async () => {
    bridge({ setup_status: { completed: false, first_run: true, state: { download_model: { model: 'downloaded:4b', provider_id: 'ollama' }, scan: { recommendations: [{ model: 'installed:4b', provider_id: 'ollama', size_bytes: 4e9, memory_available_now: false }] } }, jobs: [], catalog: [] } });
    const { change } = wizard();
    await userEvent.click(await screen.findByRole('button', { name: '2Engine & model' }));
    await screen.findByRole('button', { name: 'Use downloaded model' });
    expect(screen.getByText(/Available memory is currently limited/)).toBeTruthy();
    expect(change).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole('button', { name: 'Use downloaded model' }));
    expect(change).toHaveBeenCalledExactlyOnceWith({ model: 'downloaded:4b', provider_id: 'ollama' });
  });
});

describe('setup explicit verification', () => {
  it('runs a disposable browser check only after the button and displays actual evidence', async () => {
    const { call, responses } = bridge({ setup_browser_verify: () => { responses.setup_status = { completed: false, first_run: true, state: { browser_verify: { status: 'passed', verified: true, evidence: 'Disposable native window inspected the generated local page.' } }, jobs: [], catalog: [] }; return { id: 'browser_fixture' }; } });
    wizard(); await userEvent.click(await screen.findByRole('button', { name: '4Verify' }));
    expect(call.mock.calls.some(([action]) => action === 'setup_browser_verify' || action === 'dictation_start')).toBe(false);
    await userEvent.click(screen.getByRole('button', { name: 'Test browser' }));
    expect(call).toHaveBeenCalledWith('setup_browser_verify', {});
    expect(await screen.findByText('Disposable native window inspected the generated local page.')).toBeTruthy();
    expect(screen.getByText(/Short model probes do not qualify the full context window/)).toBeTruthy();
  });

  it('starts and stops microphone capture explicitly and confirms without persisting edited text', async () => {
    const { call } = bridge({ dictation_start: { state: 'recording' }, dictation_stop: { state: 'done', id: 'speech_fixture', text: 'Original local phrase' }, setup_dictation_confirm: { ok: true } });
    wizard(); await userEvent.click(await screen.findByRole('button', { name: '4Verify' }));
    expect(call.mock.calls.some(([action]) => action.startsWith('dictation_'))).toBe(false);
    await userEvent.click(screen.getByRole('button', { name: 'Test microphone' }));
    await userEvent.click(await screen.findByRole('button', { name: 'Stop microphone test' }));
    const transcript = await screen.findByLabelText('Test transcript');
    await userEvent.clear(transcript);
    expect((screen.getByRole('button', { name: 'Confirm microphone transcript' }) as HTMLButtonElement).disabled).toBe(true);
    await userEvent.type(transcript, 'Edited local phrase');
    await userEvent.click(screen.getByRole('button', { name: 'Confirm microphone transcript' }));
    expect(call).toHaveBeenCalledWith('setup_dictation_confirm', { id: 'speech_fixture', accepted: true });
    expect(JSON.stringify(call.mock.calls)).not.toContain('Edited local phrase');
    expect(await screen.findByRole('button', { name: 'Transcript confirmed' })).toBeTruthy();
    expect(call.mock.calls.some(([action]) => action === 'start_chat')).toBe(false);
  });

  it('cancels a microphone start that returns after the wizard has closed', async () => {
    let resolve!: (value: unknown) => void;
    const { call } = bridge({ dictation_start: () => new Promise(done => { resolve = done; }), dictation_cancel: { state: 'cancelled' } });
    const { unmount } = wizard(); await userEvent.click(await screen.findByRole('button', { name: '4Verify' }));
    await userEvent.click(screen.getByRole('button', { name: 'Test microphone' }));
    unmount();
    await act(async () => { resolve({ state: 'recording' }); });
    await waitFor(() => expect(call.mock.calls.filter(([action]) => action === 'dictation_cancel').length).toBeGreaterThanOrEqual(1));
  });

  it('keeps per-model failed and unsupported probes visible with repair and measured input', async () => {
    bridge({ setup_status: { completed: false, first_run: true, state: { verify: { model: 'previous:4b', provider_id: 'ollama', context: 8192, results: [{ case: 'tools', status: 'failed', repair: 'Reconnect the engine and rerun.' }, { case: 'vision', status: 'unsupported' }, { case: 'context', status: 'passed', input_tokens: 1700 }] } }, jobs: [], catalog: [] } });
    wizard(); await userEvent.click(await screen.findByRole('button', { name: '4Verify' }));
    expect(await screen.findByText('Reconnect the engine and rerun.')).toBeTruthy();
    expect(screen.getByText('unsupported')).toBeTruthy();
    expect(screen.getByText(/1,700 input tokens measured/)).toBeTruthy();
    expect(screen.getByText(/Selection changed; rerun verification/)).toBeTruthy();
  });
});
