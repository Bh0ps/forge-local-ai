import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { McpSettings } from '../mcpSettings';

describe('MCP editor credential boundaries', () => {
  it('sends editable fields without copying old headers, environment or authentication metadata to a new target', async () => {
    const server = { id: 'fixture', name: 'Research tools', url: 'https://first.example/mcp', transport: 'http', auth_type: 'bearer',
      enabled: true, authenticated: true, headers: { 'X-Unknown-Account': 'synthetic-old-account' },
      env: { UNCLASSIFIED: 'synthetic-old-account' }, oauth_redirect_uri: 'http://127.0.0.1:1/callback' };
    const call = vi.fn(async (action: string, data: Record<string, unknown> = {}) => {
      if (action === 'mcp_servers') return { servers: [server] };
      if (action === 'mcp_save') return { server: { ...data, id: 'fixture' } };
      if (action === 'mcp_probe') return { tool_count: 1, tools: [{ name: 'lookup' }] };
      throw new Error(`Unexpected action: ${action}`);
    });
    window.pywebview = { api: { call } };
    render(<McpSettings notify={vi.fn()} />);
    await userEvent.click(await screen.findByRole('button', { name: 'Configure Research tools' }));
    fireEvent.change(screen.getByLabelText('Endpoint URL'), { target: { value: 'https://second.example/mcp' } });
    await userEvent.click(screen.getByRole('button', { name: 'Test connection' }));
    await screen.findByText('1 tool is available.');
    expect(call).toHaveBeenCalledWith('mcp_probe', expect.objectContaining({ id: 'fixture', token: undefined, draft: { name: 'Research tools', url: 'https://second.example/mcp',
      transport: 'http', command: undefined, args: [], cwd: undefined, auth_type: 'bearer' } }));
    expect(call.mock.calls.some(([action]) => action === 'mcp_save')).toBe(false);
    const saved = call.mock.calls.find(([action]) => action === 'mcp_probe')![1]!.draft as Record<string, unknown>;
    for (const key of ['headers', 'env', 'authenticated', 'oauth_redirect_uri']) expect(key in saved).toBe(false);
  });

  it('clears typed tokens when the target, transport or authentication mode changes', async () => {
    const call = vi.fn(async (action: string, data: Record<string, unknown> = {}) => {
      if (action === 'mcp_servers') return { servers: [] };
      if (action === 'mcp_save') return { server: { ...data, id: 'fixture' } };
      if (action === 'mcp_probe') return { tool_count: 0, tools: [] };
      throw new Error(`Unexpected action: ${action}`);
    });
    window.pywebview = { api: { call } };
    render(<McpSettings notify={vi.fn()} />);
    await userEvent.click(screen.getByRole('button', { name: 'Add connection' }));
    await userEvent.type(screen.getByLabelText('Name'), 'Fixture');
    await userEvent.selectOptions(screen.getByLabelText('Authentication'), 'bearer');
    const password = screen.getByLabelText('Access token') as HTMLInputElement;
    await userEvent.type(password, 'synthetic-only');
    fireEvent.change(screen.getByLabelText('Endpoint URL'), { target: { value: 'https://second.example/mcp' } });
    expect(password.value).toBe('');
    await userEvent.type(password, 'synthetic-only');
    await userEvent.selectOptions(screen.getByLabelText('Transport'), 'sse');
    expect(password.value).toBe('');
    await userEvent.type(password, 'synthetic-only');
    await userEvent.selectOptions(screen.getByLabelText('Authentication'), 'none');
    await userEvent.selectOptions(screen.getByLabelText('Authentication'), 'bearer');
    expect((screen.getByLabelText('Access token') as HTMLInputElement).value).toBe('');
    await userEvent.type(screen.getByLabelText('Access token'), 'synthetic-only');
    await userEvent.click(screen.getByRole('button', { name: 'Advanced: local executable' }));
    await userEvent.click(screen.getByRole('button', { name: 'Use a remote URL' }));
    await userEvent.selectOptions(screen.getByLabelText('Authentication'), 'bearer');
    expect((screen.getByLabelText('Access token') as HTMLInputElement).value).toBe('');
    await userEvent.click(screen.getByRole('button', { name: 'Test connection' }));
    await waitFor(() => expect(call).toHaveBeenCalledWith('mcp_probe', expect.objectContaining({ token: undefined })));
  });

  it('identifies existing legacy authentication without changing its saved credential mode', async () => {
    const call = vi.fn(async (action: string) => action === 'mcp_servers' ? { servers: [{ id: 'legacy', name: 'Legacy tools',
      url: 'https://first.example/mcp', transport: 'http', enabled: true, authenticated: true }] } : { tool_count: 0, tools: [] });
    window.pywebview = { api: { call } };
    render(<McpSettings notify={vi.fn()} />);
    await userEvent.click(await screen.findByRole('button', { name: 'Configure Legacy tools' }));
    expect((screen.getByLabelText('Authentication') as HTMLSelectElement).value).toBe('saved');
    expect(screen.getByText('Saved credentials apply only to the original endpoint and transport.')).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: 'Test connection' }));
    await waitFor(() => expect(call).toHaveBeenCalledWith('mcp_probe', expect.objectContaining({ id: 'legacy', draft: expect.objectContaining({ auth_type: undefined }) })));
    expect(call.mock.calls.some(([action]) => action === 'mcp_save')).toBe(false);
  });
});
