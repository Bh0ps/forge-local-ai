import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import App, { applyEvents } from '../App';
import { api, friendlyModel } from '../api';
import { DEFAULT_SETTINGS } from '../types';
import { PluginsPage } from '../pluginsPage';
import { McpSettings } from '../mcpSettings';
import { ManagedRuntimes } from '../runtimeSettings';

function bridge(overrides: Record<string, unknown | ((data: Record<string, unknown>) => unknown)> = {}) {
  const responses: Record<string, unknown> = { bootstrap: { settings: { ...DEFAULT_SETTINGS, model: 'vision-coder:9b', timezone: 'Europe/Dublin' }, projects: [], chats: [], runs: [], capabilities: { mcp: true, computer: true } }, models: { models: [{ name: 'vision-coder:9b', size: 5e9 }, { name: 'large-coder:27b' }] }, settings: DEFAULT_SETTINGS, projects: { projects: [] }, chats: { chats: [] }, spaces: { spaces: [] }, agents: { agents: [] }, schedules: { schedules: [] }, providers: { providers: [] }, plugins: { plugins: [] }, skills: { skills: [] }, catalogs: { catalogs: [] }, runs: { runs: [] }, show: { capabilities: ['vision', 'tools'], model_info: { 'test.context_length': 131072 } }, usage: { totals: { input_tokens: 20, output_tokens: 10, requests: 1 }, daily: [], average_tps: 15 }, ...overrides };
  const call = vi.fn(async (action: string, data: Record<string, unknown> = {}) => { if (!(action in responses)) throw new Error(`Unexpected action: ${action}`); const result = responses[action]; return typeof result === 'function' ? result(data) : result; });
  const mode = vi.fn(async () => ({ ok: true })); window.pywebview = { api: { call, mode } }; return { call, mode, responses };
}
async function ready() { await act(async () => { window.dispatchEvent(new Event('pywebviewready')); }); await screen.findByText('What are we building?'); }

describe('Forge workspace', () => {
  beforeEach(() => { vi.clearAllMocks(); });
  it('keeps the model list closed and loads capabilities only when opened', async () => {
    const { call } = bridge(); render(<App />); await ready();
    expect(screen.queryByRole('dialog', { name: 'Choose model' })).toBeNull(); expect(call.mock.calls.some(([action]) => action === 'show')).toBe(false);
    await userEvent.click(screen.getByRole('button', { name: 'vision coder:9b' }));
    expect(await screen.findByRole('dialog', { name: 'Choose model' })).toBeTruthy(); await waitFor(() => expect(screen.getAllByText('Vision').length).toBe(2));
    expect(screen.getAllByText('128K context').length).toBe(2);
  });
  it('preserves a draft across navigation and HUD switching', async () => {
    const { mode } = bridge(); render(<App />); await ready();
    await userEvent.type(screen.getByRole('textbox', { name: 'Message Forge' }), 'Keep this draft');
    await userEvent.click(screen.getByRole('button', { name: 'Spaces' })); await screen.findByRole('heading', { name: 'Spaces' });
    await userEvent.click(screen.getByRole('button', { name: /New chat/ })); expect((screen.getByRole('textbox', { name: 'Message Forge' }) as HTMLTextAreaElement).value).toBe('Keep this draft');
    await userEvent.click(screen.getByRole('button', { name: 'Minimal HUD' })); await waitFor(() => expect(mode).toHaveBeenCalledWith('hud', false));
    expect((screen.getByRole('textbox', { name: 'Message Forge' }) as HTMLTextAreaElement).value).toBe('Keep this draft');
    await userEvent.click(screen.getByRole('button', { name: 'Full workspace' })); expect(mode).toHaveBeenCalledWith('full', false);
  });
  it('persists the exact 256K slider selection and retains its 2K minimum', async () => {
    const { call } = bridge({ settings: (data: Record<string, unknown>) => ({ ...DEFAULT_SETTINGS, ...data }) }); render(<App />); await ready();
    await userEvent.click(screen.getByRole('button', { name: 'Settings' })); const range = screen.getByRole('slider', { name: /Context window/ }) as HTMLInputElement;
    expect(range.min).toBe('2048'); expect(range.max).toBe('262144'); expect(range.step).toBe('2048');
    fireEvent.change(range, { target: { value: '262144' } }); await waitFor(() => expect(call).toHaveBeenCalledWith('settings', { context: 262144 }));
  });
  it('keeps newer context selections while older queued saves return', async () => {
    const pending: { context: number; resolve: (value: unknown) => void }[] = [];
    bridge({ settings: (data: Record<string, unknown>) => new Promise(resolve => pending.push({ context: Number(data.context), resolve })) }); render(<App />); await ready();
    await userEvent.click(screen.getByRole('button', { name: 'Settings' })); const range = screen.getByRole('slider', { name: /Context window/ }) as HTMLInputElement;
    fireEvent.change(range, { target: { value: '4096' } }); fireEvent.change(range, { target: { value: '8192' } });
    await waitFor(() => expect(pending.length).toBe(1)); await act(async () => { pending.shift()!.resolve({ ...DEFAULT_SETTINGS, context: 4096 }); });
    await waitFor(() => expect(pending.length).toBe(1)); expect(range.value).toBe('8192');
    fireEvent.change(range, { target: { value: '32768' } }); await act(async () => { pending.shift()!.resolve({ ...DEFAULT_SETTINGS, context: 8192 }); });
    await waitFor(() => expect(pending.length).toBe(1)); expect(range.value).toBe('32768');
    await act(async () => { const final = pending.shift()!; expect(final.context).toBe(32768); final.resolve({ ...DEFAULT_SETTINGS, context: final.context }); }); expect(range.value).toBe('32768');
  });
  it('never silently selects the first model when no preference was saved', async () => {
    bridge({ bootstrap: { settings: DEFAULT_SETTINGS, projects: [], chats: [], runs: [], capabilities: {} } }); render(<App />); await ready();
    expect(screen.getByRole('button', { name: 'Select model' })).toBeTruthy(); await userEvent.type(screen.getByRole('textbox', { name: 'Message Forge' }), 'Hello'); expect((screen.getByRole('button', { name: 'Send message' }) as HTMLButtonElement).disabled).toBe(true);
  });
  it('routes /todo through the shared command registry and opens the durable goal panel', async () => {
    const { call } = bridge({ command: { id: 'todo-run', chat_id: 'todo-chat', status: 'running', mode: 'goal' }, poll: { events: [], finished: false, status: 'running' }, goals: { goals: [] } }); render(<App />); await ready();
    await userEvent.type(screen.getByRole('textbox', { name: 'Message Forge' }), '/todo Build the feature'); await userEvent.click(screen.getByRole('button', { name: 'Send message' }));
    await waitFor(() => expect(call).toHaveBeenCalledWith('command', expect.objectContaining({ text: '/todo Build the feature' }))); expect(await screen.findByText('Durable goals')).toBeTruthy(); expect(screen.getByRole('button', { name: 'Stop generation' })).toBeTruthy();
  });
  it('rejects backend errors instead of showing a fake success', async () => {
    bridge({ bad_action: { error: 'Permission denied' } }); await expect(api('bad_action')).rejects.toThrow('Permission denied');
  });
});

describe('replayable run events', () => {
  const initial = { run: { id: 'run-1' }, text: '', thinking: '', parts: [], tools: [], cursor: 0, status: 'Running', finished: false };
  it('explains engine loading until the first model token even when status includes an ellipsis', () => {
    const waiting = applyEvents(initial, [{ seq: 1, type: 'status', text: 'Working…' }], {}); expect(waiting.status).toBe('Loading model / waiting for engine…');
    const generating = applyEvents(waiting, [{ seq: 2, type: 'token', text: 'Ready' }], {}); expect(generating.status).toBe('Generating');
  });
  it('deduplicates replay, preserves text and binds the exact approval', () => {
    const events = [{ seq: 1, type: 'thinking', text: 'Inspecting.' }, { seq: 2, type: 'token', text: 'Hello' }, { seq: 3, type: 'approval', approval_id: 'approved-3', name: 'write_file', arguments: { path: 'test.py' } }];
    const first = applyEvents(initial, events, { next_cursor: 3, status: 'awaiting_approval' }); const replay = applyEvents(first, events, { next_cursor: 3 });
    expect(replay.text).toBe('Hello'); expect(replay.thinking).toBe('Inspecting.'); expect(replay.approval).toEqual({ job_id: 'run-1', approval_id: 'approved-3', name: 'write_file', arguments: { path: 'test.py' }, command: undefined });
  });
  it('retains round output, exact speed and a resumable pause', () => {
    const next = applyEvents(initial, [{ seq: 1, type: 'token', text: 'First round' }, { seq: 2, type: 'round' }, { seq: 3, type: 'token', text: 'Second round' }, { seq: 4, type: 'usage', tps: 42.5 }, { seq: 5, type: 'done', paused: true }], { finished: true, status: 'paused', recovery: 'Output limit' });
    expect(next.parts[0].content).toBe('First round'); expect(next.text).toBe('Second round'); expect(next.speed).toBe(42.5); expect(next.run.status).toBe('paused'); expect(next.run.recovery).toBe('Output limit');
  });
  it('uses a compact model name without dropping its size variant', () => { expect(friendlyModel('community/vision-coder:9b')).toBe('vision coder:9b'); expect(friendlyModel('coder:latest')).toBe('coder'); });
});

describe('integration setup boundaries', () => {
  it('refreshes the coordinator-registered runtime endpoint without inventing capabilities or a provider', async () => {
    const { call } = bridge({ runtime_status: { runtimes: [{ id: 'local-runtime', engine: 'llama.cpp', version: 'b11413', executable: '/local/llama-server.exe', state: 'running', last_launch: { url: 'http://127.0.0.1:8082/v1' } }] }, providers: { providers: [{ id: 'runtime-local-runtime', capabilities: [], context_limit: 32768 }] } });
    const refresh = vi.fn(async () => {}); render(<ManagedRuntimes settings={DEFAULT_SETTINGS} notify={vi.fn()} onRefresh={refresh} />);
    await userEvent.click(await screen.findByRole('button', { name: 'Refresh endpoint' }));
    await waitFor(() => expect(refresh).toHaveBeenCalledOnce()); expect(call).toHaveBeenCalledWith('providers', {}); expect(call.mock.calls.some(([action]) => action === 'provider_save')).toBe(false);
  });
  it('registers the verified runtime contract without starting it implicitly', async () => {
    const { call } = bridge({ runtime_status: { runtimes: [], profiles: [{ engine: 'llama.cpp', version: 'b11413' }] }, runtime_install: { id: 'local-runtime' } });
    render(<ManagedRuntimes settings={DEFAULT_SETTINGS} notify={vi.fn()} onRefresh={vi.fn(async () => {})} />);
    await userEvent.click(screen.getByRole('button', { name: 'Add runtime' })); await userEvent.type(screen.getByLabelText('Executable path'), '/local/llama-server.exe'); await userEvent.click(screen.getByRole('button', { name: 'Register runtime' }));
    await waitFor(() => expect(call).toHaveBeenCalledWith('runtime_install', { engine: 'llama.cpp', executable: '/local/llama-server.exe', url: undefined, sha256: undefined })); expect(call.mock.calls.some(([action]) => action === 'runtime_start')).toBe(false);
  });
  it('installs exactly the staged plugin that was reviewed', async () => {
    const { call } = bridge({ plugin_inspect: { name: 'Review', compatible: true, inspection_id: 'staged-1', compatibility: 'Portable skills', licenses: ['LICENSE'] }, plugin_install: { ok: true } });
    render(<PluginsPage notify={vi.fn()} />); await userEvent.click(screen.getByRole('button', { name: 'Import plugin' })); await userEvent.type(screen.getByLabelText('Source'), 'https://github.com/example/review'); await userEvent.click(screen.getByRole('button', { name: 'Inspect source' })); await screen.findByText('Portable skills'); await userEvent.click(screen.getByRole('button', { name: 'Install reviewed plugin' })); await waitFor(() => expect(call).toHaveBeenCalledWith('plugin_install', { inspection_id: 'staged-1', source: 'https://github.com/example/review', reviewed: true }));
  });
  it('saves an MCP connection disabled, tests its ID, then enables it', async () => {
    const { call } = bridge({ mcp_servers: { servers: [] }, mcp_save: (data: Record<string, unknown>) => ({ server: { ...data, id: 'mcp-1' } }), mcp_test: { server: { id: 'mcp-1' }, tools: [{ name: 'lookup' }] } });
    render(<McpSettings notify={vi.fn()} />); await userEvent.click(screen.getByRole('button', { name: 'Add connection' })); await userEvent.type(screen.getByLabelText('Name'), 'Research tools'); await userEvent.type(screen.getByLabelText('Endpoint URL'), 'https://example.com/mcp'); await userEvent.click(screen.getByRole('button', { name: 'Connect / Test' })); await screen.findByText('Connected. 1 tools available.');
    expect(call).toHaveBeenCalledWith('mcp_save', expect.objectContaining({ enabled: false, url: 'https://example.com/mcp' })); expect(call).toHaveBeenCalledWith('mcp_test', { id: 'mcp-1' }); await userEvent.click(screen.getByRole('button', { name: 'Enable connection' })); await waitFor(() => expect(call).toHaveBeenCalledWith('mcp_save', { id: 'mcp-1', enabled: true }));
  });
});
