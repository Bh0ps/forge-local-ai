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
      if (action === 'mcp_test') return { tools: [{ name: 'lookup' }] };
      throw new Error(`Unexpected action: ${action}`);
    });
    window.pywebview = { api: { call } };
    render(<McpSettings notify={vi.fn()} />);
    await userEvent.click(await screen.findByRole('button', { name: 'Edit' }));
    fireEvent.change(screen.getByLabelText('Endpoint URL'), { target: { value: 'https://second.example/mcp' } });
    await userEvent.click(screen.getByRole('button', { name: 'Connect / Test' }));
    await screen.findByText('Connected. 1 tools available.');
    expect(call).toHaveBeenCalledWith('mcp_save', { id: 'fixture', name: 'Research tools', url: 'https://second.example/mcp',
      transport: 'http', command: undefined, args: [], auth_type: 'bearer', token: undefined, enabled: false });
    const saved = call.mock.calls.find(([action]) => action === 'mcp_save')![1]!;
    for (const key of ['headers', 'env', 'authenticated', 'oauth_redirect_uri']) expect(key in saved).toBe(false);
  });

  it('clears typed tokens when the target, transport or authentication mode changes', async () => {
    const call = vi.fn(async (action: string, data: Record<string, unknown> = {}) => {
      if (action === 'mcp_servers') return { servers: [] };
      if (action === 'mcp_save') return { server: { ...data, id: 'fixture' } };
      if (action === 'mcp_test') return { tools: [] };
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
    expect((screen.getByLabelText('Access token') as HTMLInputElement).value).toBe('');
    await userEvent.click(screen.getByRole('button', { name: 'Connect / Test' }));
    await waitFor(() => expect(call).toHaveBeenCalledWith('mcp_save', expect.objectContaining({ token: undefined })));
  });
});
