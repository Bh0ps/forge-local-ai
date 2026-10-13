import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { McpEditor, McpSettings, type McpServer } from '../mcpSettings';

const fixture: McpServer = { id: 'fixture', name: 'Research tools', url: 'https://first.example/mcp', transport: 'http', auth_type: 'none', enabled: true, status: 'ready', tools: [{ name: 'lookup' }] };
const deferred = () => { let resolve!: (value: unknown) => void; const promise = new Promise<unknown>(done => { resolve = done; }); return { promise, resolve }; };
function bridge(overrides: Record<string, unknown> = {}) {
  const responses: Record<string, unknown> = { mcp_servers: { servers: [fixture] }, mcp_probe: { tool_count: 1, tools: [{ name: 'lookup' }] }, mcp_probe_cancel: { ok: true }, mcp_auth_cancel: { ok: true }, mcp_save: { server: fixture }, mcp_test: { tools: fixture.tools, setup_token: 'synthetic-setup' }, ...overrides };
  const call = vi.fn(async (action: string, data: Record<string, unknown>) => {
    if (!(action in responses)) throw new Error(`Unexpected action: ${action}`);
    const value = responses[action]; return typeof value === 'function' ? value(data) : value;
  });
  window.pywebview = { api: { call } }; return call;
}
function editor(server: McpServer = fixture) {
  const close = vi.fn(), committed = vi.fn(), complete = vi.fn();
  const view = render(<McpEditor server={server} close={close} committed={committed} complete={complete} />);
  return { ...view, close, committed, complete };
}

describe('MCP setup lifecycle', () => {
  it('checks an existing connection without saving, disabling or reopening it after cancellation', async () => {
    const checked = deferred(); const call = bridge({ mcp_probe: () => checked.promise });
    render(<McpSettings notify={vi.fn()} />);
    await userEvent.click(await screen.findByRole('button', { name: 'Configure Research tools' }));
    fireEvent.click(screen.getByRole('button', { name: 'Test connection' }));
    fireEvent.click(screen.getByRole('button', { name: 'Test connection' }));
    expect(call.mock.calls.filter(([action]) => action === 'mcp_probe')).toHaveLength(1);
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    await userEvent.click(screen.getByRole('button', { name: 'Add connection' }));
    await act(async () => checked.resolve({ tool_count: 5 }));
    expect(screen.getByRole('dialog', { name: 'Add MCP connection' })).toBeTruthy();
    expect(screen.queryByText('Connection verified')).toBeNull();
    expect(screen.getByText('Enabled')).toBeTruthy();
    expect(call.mock.calls.some(([action]) => ['mcp_save', 'mcp_test', 'mcp_auth_cancel'].includes(action))).toBe(false);
    expect(call).toHaveBeenCalledWith('mcp_probe_cancel', { probe_id: expect.any(String) });
  });

  it('does not submit an unverified connection with Enter', () => {
    const call = bridge(); editor();
    fireEvent.submit(screen.getByLabelText('Name').closest('form')!);
    expect(call).not.toHaveBeenCalled();
  });

  it('saves once and waits for a fresh saved catalog before enabling', async () => {
    const save = deferred(), check = deferred();
    const call = bridge({ mcp_save: (data: Record<string, unknown>) => data.enabled ? { server: fixture } : save.promise, mcp_test: () => check.promise });
    const { complete } = editor({ ...fixture, id: '', enabled: false });
    await userEvent.click(screen.getByRole('button', { name: 'Test connection' }));
    await screen.findByText('Connection verified');
    const form = screen.getByLabelText('Name').closest('form')!;
    fireEvent.submit(form); fireEvent.submit(form);
    expect(call.mock.calls.filter(([action]) => action === 'mcp_save')).toHaveLength(1);
    expect(call.mock.calls.find(([action]) => action === 'mcp_save')![1]).toEqual(expect.objectContaining({ id: expect.stringMatching(/^[a-f0-9]{32}$/), enabled: false }));
    await act(async () => save.resolve({ server: fixture }));
    expect(call).toHaveBeenCalledWith('mcp_test', { id: 'fixture' });
    expect(call.mock.calls.some(([action, data]) => action === 'mcp_save' && data.enabled)).toBe(false);
    await act(async () => check.resolve({ tools: fixture.tools, setup_token: 'synthetic-setup' }));
    expect(call).toHaveBeenCalledWith('mcp_save', { id: 'fixture', enabled: true, setup_token: 'synthetic-setup' });
    expect(complete).toHaveBeenCalledOnce();
  });

  it('canceling an explicit save stops verification and leaves the newer editor untouched', async () => {
    const save = deferred(); const call = bridge({ mcp_save: () => save.promise });
    render(<McpSettings notify={vi.fn()} />);
    await userEvent.click(await screen.findByRole('button', { name: 'Configure Research tools' }));
    await userEvent.click(screen.getByRole('button', { name: 'Test connection' }));
    await screen.findByText('Connection verified');
    await userEvent.click(screen.getByRole('button', { name: 'Save & enable' }));
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    await userEvent.click(screen.getByRole('button', { name: 'Add connection' }));
    await act(async () => save.resolve({ server: fixture }));
    expect(screen.getByRole('dialog', { name: 'Add MCP connection' })).toBeTruthy();
    expect(call.mock.calls.some(([action]) => action === 'mcp_test')).toBe(false);
  });

  it('keeps a saved verification failure disabled and gives a usable retry', async () => {
    const call = bridge({ mcp_test: { error: 'Connection unavailable. Check the endpoint and try again.' } });
    const { complete } = editor();
    await userEvent.click(screen.getByRole('button', { name: 'Test connection' }));
    await screen.findByText('Connection verified');
    await userEvent.click(screen.getByRole('button', { name: 'Save & enable' }));
    expect((await screen.findByRole('alert')).textContent).toContain('Connection unavailable.');
    expect(screen.getByText('The connection was saved. Its tools stay disabled until setup finishes.')).toBeTruthy();
    expect(call.mock.calls.some(([action, data]) => action === 'mcp_save' && data.enabled)).toBe(false);
    expect(complete).not.toHaveBeenCalled();
    expect((screen.getByRole('button', { name: 'Test connection' }) as HTMLButtonElement).disabled).toBe(false);
  });

  it('starts sign-in only after an explicit save and ignores a canceled late start', async () => {
    const start = deferred(); const call = bridge({ mcp_auth_start: () => start.promise });
    const { close, complete } = editor({ ...fixture, auth_type: 'oauth' });
    await userEvent.click(screen.getByRole('button', { name: 'Save & sign in' }));
    const attempt = call.mock.calls.find(([action]) => action === 'mcp_auth_start')![1];
    expect(call.mock.calls.findIndex(([action]) => action === 'mcp_save')).toBeLessThan(call.mock.calls.findIndex(([action]) => action === 'mcp_auth_start'));
    await userEvent.click(screen.getByRole('button', { name: 'Close' }));
    await act(async () => start.resolve({ state: 'connecting' }));
    expect(close).toHaveBeenCalledOnce(); expect(complete).not.toHaveBeenCalled();
    expect(call).toHaveBeenCalledWith('mcp_auth_cancel', attempt);
    expect(call.mock.calls.some(([action]) => action === 'mcp_auth_status')).toBe(false);
  });

  it('ignores old sign-in status after canceling and changing the endpoint', async () => {
    const status = deferred(); const call = bridge({ mcp_auth_start: { state: 'connecting' }, mcp_auth_status: () => status.promise });
    const { complete } = editor({ ...fixture, auth_type: 'oauth' });
    await userEvent.click(screen.getByRole('button', { name: 'Save & sign in' }));
    await waitFor(() => expect(call.mock.calls.some(([action]) => action === 'mcp_auth_status')).toBe(true));
    await userEvent.click(screen.getByRole('button', { name: 'Cancel sign-in' }));
    fireEvent.change(screen.getByLabelText('Endpoint URL'), { target: { value: 'https://second.example/mcp' } });
    await act(async () => status.resolve({ state: 'ready', server: { ...fixture, tools: [{ name: 'stale-tool' }] } }));
    expect(screen.queryByText('Signed in')).toBeNull(); expect(screen.queryByText('stale-tool')).toBeNull();
    expect((screen.getByLabelText('Endpoint URL') as HTMLInputElement).value).toBe('https://second.example/mcp');
    expect(complete).not.toHaveBeenCalled();
    expect(call.mock.calls.some(([action, data]) => action === 'mcp_save' && data.enabled)).toBe(false);
  });

  it('does not duplicate browser sign-in on Enter while waiting, and requires explicit enablement', async () => {
    const status = deferred(); const call = bridge({ mcp_auth_start: { state: 'connecting' }, mcp_auth_status: () => status.promise });
    const { complete } = editor({ ...fixture, auth_type: 'oauth' });
    await userEvent.click(screen.getByRole('button', { name: 'Save & sign in' }));
    fireEvent.submit(screen.getByLabelText('Name').closest('form')!);
    expect(call.mock.calls.filter(([action]) => action === 'mcp_auth_start')).toHaveLength(1);
    await waitFor(() => expect(call.mock.calls.some(([action]) => action === 'mcp_auth_status')).toBe(true));
    await act(async () => status.resolve({ state: 'ready', server: fixture, setup_token: 'synthetic-setup' }));
    expect(screen.getByText('Signed in')).toBeTruthy(); expect(complete).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole('button', { name: 'Enable connection' }));
    expect(call).toHaveBeenCalledWith('mcp_save', { id: 'fixture', enabled: true, setup_token: 'synthetic-setup' });
    expect(complete).toHaveBeenCalledOnce();
  });

  it('rejects malformed check output and presents an honest empty catalog', async () => {
    const call = bridge({ mcp_probe: { tool_count: -1 } }); editor();
    await userEvent.click(screen.getByRole('button', { name: 'Test connection' }));
    expect((await screen.findByRole('alert')).textContent).toContain('usable tool catalog');
    expect((screen.getByRole('button', { name: 'Save & enable' }) as HTMLButtonElement).disabled).toBe(true);
    window.pywebview!.api.call = vi.fn(async () => ({ tool_count: 0, tools: [] }));
    await userEvent.click(screen.getByRole('button', { name: 'Test connection' }));
    expect(await screen.findByText(/This server does not advertise tools yet/)).toBeTruthy();
    expect(call.mock.calls.some(([action]) => action === 'mcp_save')).toBe(false);
  });

  it('does not report sign-in success without a usable saved tool catalog', async () => {
    const call = bridge({ mcp_auth_start: { state: 'connecting' }, mcp_auth_status: { state: 'ready' } });
    const { complete } = editor({ ...fixture, auth_type: 'oauth' });
    await userEvent.click(screen.getByRole('button', { name: 'Save & sign in' }));
    expect((await screen.findByRole('alert')).textContent).toContain('usable tool catalog');
    expect(screen.queryByText('Signed in')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Enable connection' })).toBeNull();
    expect(call).toHaveBeenCalledWith('mcp_auth_cancel', expect.objectContaining({ id: 'fixture', flow_id: expect.any(String) }));
    expect(complete).not.toHaveBeenCalled();
  });

  it('requires another sign-in check when activation rejects an outdated saved revision', async () => {
    const call = bridge({ mcp_auth_start: { state: 'connecting' }, mcp_auth_status: { state: 'ready', server: fixture, setup_token: 'synthetic-setup' },
      mcp_save: (data: Record<string, unknown>) => data.enabled ? { error: 'This connection changed during setup. Check it again before enabling.' } : { server: fixture } });
    const { complete } = editor({ ...fixture, auth_type: 'oauth' });
    await userEvent.click(screen.getByRole('button', { name: 'Save & sign in' }));
    await screen.findByText('Signed in');
    await userEvent.click(screen.getByRole('button', { name: 'Enable connection' }));
    expect((await screen.findByRole('alert')).textContent).toContain('changed during setup');
    expect(screen.queryByText('Signed in')).toBeNull();
    expect(screen.getByRole('button', { name: 'Save & sign in' })).toBeTruthy();
    expect(call).toHaveBeenCalledWith('mcp_auth_cancel', expect.objectContaining({ id: 'fixture', flow_id: expect.any(String) }));
    expect(complete).not.toHaveBeenCalled();
  });
});
