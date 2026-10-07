import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import App from './App';
import { ChannelSettings, NotificationCenter } from './channelSettings';
import { MemoryPage } from './memoryPage';
import { SetupWizard } from './setupWizard';
import { UpdateSettings } from './updateSettings';
import { OpenRouterSettings } from './openRouterSettings';
import { AgentProviderFields } from './agentProviderFields';
import { DEFAULT_SETTINGS } from './types';

const settings = { ...DEFAULT_SETTINGS, model: 'fixture:9b', context: 65536, thinking: false };
const models = [{ name: 'fixture:9b', size: 5e9 }];
const project = { id: 'project_fixture', name: 'Fixture project', path: '/fixture/project' };
const agent = { id: 'coder', name: 'Coder', instructions: 'Fixture guidance', enabled: true };
function bridge(overrides: Record<string, unknown> = {}) {
  const responses: Record<string, unknown> = {
    bootstrap: { settings, projects: [project], chats: [], runs: [], capabilities: { setup: true, memory: true, channels: true, updates: true } },
    models: { models }, projects: { projects: [project] }, chats: { chats: [] }, runs: { runs: [] },
    agents: { agents: [agent] }, providers: { providers: [] },
    setup_status: { first_run: false, completed: true, jobs: [], state: {}, catalog: [{ name: 'fixture:9b', title: 'Fixture coder', size_bytes: 5e9, description: 'Local fixture.' }] },
    memory_list: { items: [], skills: [] }, channel_list: { channels: [], pairings: [] }, channel_notifications: { notifications: [], outbox: [] },
    settings: (data: Record<string, unknown>) => ({ ...settings, ...data }),
    ...overrides,
  };
  const call = vi.fn(async (action: string, data: Record<string, unknown> = {}) => {
    if (!(action in responses)) throw new Error(`Unexpected action ${action}`);
    const result = responses[action]; return typeof result === 'function' ? result(data) : result;
  });
  const mode = vi.fn(async () => ({ ok: true }));
  window.pywebview = { api: { call, mode } };
  return { call, responses, mode };
}

describe('Forge 4.2 guided setup', () => {
  it('detects and downloads through durable jobs without replacing saved model or context', async () => {
    const { call, responses } = bridge({ setup_scan: () => { responses.setup_status = { first_run: true, completed: false, state: {}, catalog: [{ name: 'fixture:9b', title: 'Fixture coder', size_bytes: 5e9, description: 'Local fixture.' }], jobs: [{ id: 'scan_fixture', operation: 'scan', status: 'completed', result: { hardware: { cpu: { name: 'Fixture CPU', physical_cores: 8 }, ram: { total_bytes: 64e9 }, gpus: [] }, engines: [{ id: 'ollama', name: 'Ollama', online: true }] } }] }; return { id: 'scan_fixture' }; }, setup_download_model: () => { responses.setup_status = { first_run: true, completed: false, state: {}, catalog: [], jobs: [{ id: 'download_fixture', operation: 'download_model', status: 'running', phase: 'Downloading fixture', completed_bytes: 1e9, total_bytes: 5e9 }] }; return { id: 'download_fixture' }; }, setup_cancel: { ok: true } });
    const change = vi.fn(async () => {});
    render(<SetupWizard settings={settings} models={models} onChange={change} refreshModels={vi.fn(async () => {})} close={vi.fn()} openSettings={vi.fn()} />);
    await userEvent.click(await screen.findByRole('button', { name: 'Detect my computer' }));
    expect(await screen.findByText('Fixture CPU')).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: '2Engine & model' }));
    expect((screen.getByLabelText('Installed model') as HTMLSelectElement).value).toBe('fixture:9b');
    expect(screen.getByText(/64K context/)).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: 'Download' }));
    expect(await screen.findByRole('status')).toBeTruthy();
    expect(screen.getByText(/Downloading fixture/)).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(call).toHaveBeenCalledWith('setup_cancel', { id: 'download_fixture' });
    expect(call).toHaveBeenCalledWith('setup_download_model', { model: 'fixture:9b' });
    expect(change).not.toHaveBeenCalled();
  });

  it('keeps failures visible and only changes permissions following an explicit choice', async () => {
    bridge({ setup_scan: { error: 'Fixture engine is unavailable.' } });
    const change = vi.fn(async () => {});
    render(<SetupWizard settings={settings} models={models} onChange={change} refreshModels={vi.fn(async () => {})} close={vi.fn()} openSettings={vi.fn()} />);
    await userEvent.click(await screen.findByRole('button', { name: 'Detect my computer' }));
    expect(await screen.findByRole('alert')).toHaveProperty('textContent', 'Fixture engine is unavailable.');
    await userEvent.click(screen.getByRole('button', { name: '3Permissions' }));
    await userEvent.click(screen.getByRole('button', { name: /Chat only/ }));
    expect(change).toHaveBeenCalledExactlyOnceWith({ permission_profile: 'deny_access' });
  });
});

describe('reviewed memory controls', () => {
  it('proposes in the selected project and agent scope and binds approval/correction to revision', async () => {
    const memory = { id: 'memory_fixture', title: 'Formatter', content: 'Use Ruff.', kind: 'convention', scope: 'agent', status: 'pending', revision: 3, source: { kind: 'user' } };
    const { call } = bridge({ memory_list: { items: [memory], skills: [] }, memory_propose: { ok: true }, memory_review: { ok: true }, memory_update: { ok: true } });
    render(<MemoryPage projects={[project]} settings={settings} onChange={vi.fn(async () => {})} notify={vi.fn()} />);
    await screen.findByText('Formatter');
    await userEvent.selectOptions(screen.getByLabelText('Memory project'), project.id);
    await userEvent.selectOptions(screen.getByLabelText('Memory agent'), 'coder');
    await userEvent.click(screen.getByRole('button', { name: 'Approve' }));
    expect(call).toHaveBeenCalledWith('memory_review', { project_id: project.id, agent_id: 'coder', id: memory.id, revision: 3, approved: true });
    await userEvent.click(screen.getByRole('button', { name: 'Edit' }));
    const dialog = screen.getByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText('Content'), { target: { value: 'Use Ruff and format before review.' } });
    await userEvent.click(within(dialog).getByRole('button', { name: 'Save correction' }));
    await waitFor(() => expect(call).toHaveBeenCalledWith('memory_update', expect.objectContaining({ project_id: project.id, agent_id: 'coder', id: memory.id, revision: 3, content: 'Use Ruff and format before review.' })));
    await userEvent.click(screen.getByRole('button', { name: 'Suggest a save' }));
    await userEvent.type(screen.getByLabelText('Title'), 'Test conventions');
    await userEvent.type(screen.getByLabelText('Content'), 'Use synthetic inputs.');
    await userEvent.click(screen.getByRole('button', { name: 'Save for review' }));
    await waitFor(() => expect(call).toHaveBeenCalledWith('memory_propose', expect.objectContaining({ scope: 'agent', project_id: project.id, agent_id: 'coder', source: { kind: 'user' } })));
    expect(call.mock.calls.filter(([action]) => action === 'memory_propose')[0][1]).not.toHaveProperty('approved');
  });

  it('requires a concrete confirmation before forgetting selected memory', async () => {
    const { call } = bridge({ memory_list: { items: [{ id: 'memory_fixture', title: 'Old convention', content: 'Old text.', kind: 'fact', scope: 'global', status: 'approved', revision: 1 }], skills: [] }, memory_delete: { ok: true } });
    render(<MemoryPage projects={[project]} settings={settings} onChange={vi.fn(async () => {})} notify={vi.fn()} />);
    await userEvent.click(await screen.findByRole('button', { name: 'Forget Old convention' }));
    expect(call.mock.calls.some(([action]) => action === 'memory_delete')).toBe(false);
    await userEvent.click(screen.getByRole('button', { name: 'Forget' }));
    expect(call).toHaveBeenCalledWith('memory_delete', { project_id: null, agent_id: null, id: 'memory_fixture' });
  });

  it('promotes a learned skill with edited instructions, source license and explicit acceptance', async () => {
    const { call } = bridge({ memory_list: { items: [], skills: [{ id: 'skill_fixture', name: 'fixture-workflow', description: 'Review before use.', markdown: '---\nname: fixture-workflow\n---\nProvisional instructions.', scope: 'global', status: 'pending', revision: 2, license: 'Unspecified; review required' }] }, skill_promote: { ok: true } });
    render(<MemoryPage projects={[project]} settings={settings} onChange={vi.fn(async () => {})} notify={vi.fn()} />);
    await userEvent.click(await screen.findByRole('button', { name: /Learned skills/ }));
    await userEvent.click(await screen.findByRole('button', { name: 'Review skill' }));
    expect((screen.getByRole('button', { name: 'Promote skill' }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(screen.getByLabelText('Instructions'), { target: { value: 'Inspect local files, make one reviewed change, and verify it.' } });
    await userEvent.type(screen.getByLabelText('Source license'), 'MIT');
    await userEvent.click(screen.getByRole('button', { name: 'Promote skill' }));
    await waitFor(() => expect(call).toHaveBeenCalledWith('skill_promote', { project_id: null, agent_id: null, id: 'skill_fixture', revision: 2, approved: true, instructions: 'Inspect local files, make one reviewed change, and verify it.', license: 'MIT' }));
  });
});

describe('connected Telegram controls', () => {
  it('keeps the bot credential masked and sends reviewed task/profile/privacy configuration', async () => {
    const { call } = bridge({ channel_save: { channel: { id: 'channel_fixture' } } });
    render(<ChannelSettings models={models} notify={vi.fn()} />);
    await userEvent.click(await screen.findByRole('button', { name: 'Add connection' }));
    const credential = screen.getByLabelText('Bot token') as HTMLInputElement;
    expect(credential.type).toBe('password');
    await userEvent.type(credential, 'synthetic_bot_credential');
    await userEvent.selectOptions(screen.getByLabelText('Project'), project.id);
    await userEvent.selectOptions(screen.getByLabelText('Agent'), 'coder');
    await userEvent.selectOptions(screen.getByLabelText('Permission ceiling'), 'always_ask');
    expect(screen.getByRole('switch', { name: 'Allow incoming tasks' }).getAttribute('aria-checked')).toBe('true');
    expect(screen.getByRole('switch', { name: 'Include response content' }).getAttribute('aria-checked')).toBe('false');
    await userEvent.click(screen.getByRole('switch', { name: 'Enable connection' }));
    await userEvent.click(screen.getByRole('button', { name: 'Save connection' }));
    await waitFor(() => expect(call).toHaveBeenCalledWith('channel_save', expect.objectContaining({ token: 'synthetic_bot_credential', project_id: project.id, agent_profile_id: 'coder', enabled: true, inbound_tasks: true, permission_profile: 'always_ask', include_content: false })));
    expect(call.mock.calls.some(([action]) => action === 'channel_test')).toBe(false);
    expect(screen.queryByDisplayValue('synthetic_bot_credential')).toBeNull();
  });

  it('connects only explicitly, shows the pair command, approves the exact request, and revokes the exact pair', async () => {
    const channel = { id: 'channel_fixture', name: 'Fixture Telegram', kind: 'telegram', enabled: true, connected: true, allowlist: [{ sender_id: '42', chat_id: '42' }] };
    const { call } = bridge({ channel_list: { channels: [channel], pairings: [{ id: 'pairing_fixture', channel_id: channel.id, sender_id: '77', chat_id: '77', status: 'pending' }] }, channel_test: { channel }, channel_pair_begin: { code: 'fixture_pair_code' }, channel_pair_approve: { channel }, channel_pair_revoke: { channel } });
    render(<ChannelSettings models={models} notify={vi.fn()} />);
    await screen.findByText('Fixture Telegram');
    expect(call.mock.calls.some(([action]) => action === 'channel_test')).toBe(false);
    await userEvent.click(screen.getByRole('button', { name: 'Connect / Test' }));
    expect(call).toHaveBeenCalledWith('channel_test', { id: channel.id });
    await userEvent.click(screen.getByRole('button', { name: 'Pair account' }));
    expect(await screen.findByText('/pair fixture_pair_code')).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: 'Done' }));
    await userEvent.click(screen.getByRole('button', { name: 'Approve this account' }));
    expect(call).toHaveBeenCalledWith('channel_pair_approve', { id: channel.id, pairing_id: 'pairing_fixture' });
    await userEvent.click(screen.getByRole('button', { name: 'Revoke' }));
    expect(call).toHaveBeenCalledWith('channel_pair_revoke', { id: channel.id, sender_id: '42', chat_id: '42' });
  });

  it('marks a notification read before opening its associated run', async () => {
    const notification = { id: 'notification_fixture', run_id: 'run_fixture', kind: 'approval', data: { tool: 'write_file' }, unread: true, created_at: '2026-10-05T19:00:00Z' };
    const { call, responses } = bridge({ channel_notifications: { notifications: [notification] }, channel_notification_read: () => { responses.channel_notifications = { notifications: [{ ...notification, unread: false }] }; return { ok: true }; } });
    const openRun = vi.fn();
    render(<NotificationCenter openRun={openRun} close={vi.fn()} />);
    const item = await screen.findByRole('button', { name: /Approval: write_file/ });
    expect(item.classList.contains('unread')).toBe(true);
    await userEvent.click(item);
    await waitFor(() => expect(openRun).toHaveBeenCalledExactlyOnceWith('run_fixture'));
    expect(call).toHaveBeenCalledWith('channel_notification_read', { id: notification.id });
    await waitFor(() => expect(item.classList.contains('unread')).toBe(false));
  });
});

describe('update and workspace integration', () => {
  it('disables installation while signing is pending and exports privacy-safe diagnostics', async () => {
    const status = { current_version: '4.2.0', state: 'available', automatic_install_available: false, signing_setup_required: true, message: 'Fixture signing setup pending.', release: { version: '4.2.1', size: 1e6 } };
    const { call } = bridge({ update_status: status, diagnostics_export: { diagnostics: { app: { version: '4.2.0' }, counts: { chats: 2 } } } });
    const notify = vi.fn(); const urls = vi.fn(() => 'blob:fixture');
    vi.stubGlobal('URL', Object.assign(URL, { createObjectURL: urls, revokeObjectURL: vi.fn() }));
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
    render(<UpdateSettings notify={notify} />);
    const download = await screen.findByRole('button', { name: 'Download & verify' });
    expect((download as HTMLButtonElement).disabled).toBe(true);
    expect(screen.queryByRole('button', { name: 'Install & restart' })).toBeNull();
    await userEvent.click(screen.getByRole('button', { name: 'Export diagnostic report' }));
    await waitFor(() => expect(call).toHaveBeenCalledWith('diagnostics_export', {}));
    await waitFor(() => expect(notify).toHaveBeenCalledWith(expect.stringContaining('no conversations or private paths')));
    expect(call.mock.calls.some(([action]) => ['chats', 'chat', 'run', 'attachments'].includes(action))).toBe(false);
  });

  it('opens guided setup on first run and preserves a composer draft through memory and HUD navigation', async () => {
    const { mode } = bridge({ setup_status: { first_run: true, completed: false, jobs: [], state: {}, catalog: [] }, setup_skip: { ok: true } });
    render(<App />);
    await act(async () => { window.dispatchEvent(new Event('pywebviewready')); });
    await screen.findByText('Set up your Forge workspace');
    await userEvent.click(screen.getByRole('button', { name: 'Skip for now' }));
    await screen.findByText('What are we building?');
    await userEvent.type(screen.getByRole('textbox', { name: 'Message Forge' }), 'Keep my draft.');
    await userEvent.click(screen.getByRole('button', { name: 'Memory' }));
    expect(await screen.findByRole('heading', { name: 'Memory' })).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: /New chat/ }));
    expect((screen.getByRole('textbox', { name: 'Message Forge' }) as HTMLTextAreaElement).value).toBe('Keep my draft.');
    await userEvent.click(screen.getByRole('button', { name: 'Minimal HUD' }));
    expect(mode).toHaveBeenCalledWith('hud', false);
    expect((screen.getByRole('textbox', { name: 'Message Forge' }) as HTMLTextAreaElement).value).toBe('Keep my draft.');
  });
});

describe('free OpenRouter connection', () => {
  it('masks the key, requires reviewed remote controls, and tests metadata only on request', async () => {
    const provider = { enabled: false, remote_consent: false, data_collection: 'deny', connected: false };
    const { call } = bridge({ openrouter_status: { provider }, openrouter_save: (data: Record<string, unknown>) => ({ provider: { ...data, api_key: undefined } }),
      openrouter_test: { provider: { enabled: true, remote_consent: true, connected: true, data_collection: 'deny', account: { free_model_daily_requests: { used: 7, limit: 50, remaining: 43 } } } }, openrouter_setup_agents: { agents: [{ id: 'cloud_fixture' }], settings: { auto_delegate: true, goal_review_enabled: true } } });
    const notify = vi.fn();
    render(<OpenRouterSettings notify={notify} settings={DEFAULT_SETTINGS} onChange={vi.fn(async () => {})} />);
    const key = await screen.findByLabelText('OpenRouter API key') as HTMLInputElement;
    expect(key.type).toBe('password');
    expect(call.mock.calls.some(([action]) => action === 'openrouter_test')).toBe(false);
    await userEvent.type(key, 'synthetic_openrouter_fixture_key');
    await userEvent.click(screen.getByRole('switch', { name: 'Allow assigned context to leave this computer' }));
    await userEvent.click(screen.getByRole('switch', { name: 'Enable free OpenRouter agents' }));
    expect(screen.getByRole('switch', { name: 'Allow providers that collect data' }).getAttribute('aria-checked')).toBe('false');
    await userEvent.click(screen.getByRole('button', { name: 'Save connection' }));
    await waitFor(() => expect(call).toHaveBeenCalledWith('openrouter_save', expect.objectContaining({ enabled: true, remote_consent: true, data_collection: 'deny', api_key: 'synthetic_openrouter_fixture_key' })));
    expect(key.value).toBe('');
    expect(call.mock.calls.some(([action]) => action === 'openrouter_test')).toBe(false);
    await userEvent.click(screen.getByRole('button', { name: 'Connect / Test' }));
    expect(await screen.findByText(/7 \/ 50.*43 remaining/)).toBeTruthy();
    expect(call).toHaveBeenCalledWith('openrouter_test', {});
    await userEvent.click(screen.getByRole('button', { name: 'Set up helpers & reviewer' }));
    await waitFor(() => expect(call).toHaveBeenCalledWith('openrouter_setup_agents', {}));
    expect(call.mock.calls.some(([action]) => ['agent_start', 'start', 'chat_start'].includes(action))).toBe(false);
  });

  it('uses the selected cloud provider catalog for the agent without replacing the parent engine', async () => {
    const { call } = bridge({ providers: { providers: [{ id: 'ollama', name: 'Ollama' }, { id: 'openrouter', name: 'OpenRouter free' }] },
      models: { models: [{ name: 'openrouter/free' }, { name: 'fixture/model:free' }] } });
    const update = vi.fn();
    render(<AgentProviderFields agent={{ ...agent, provider_id: 'openrouter', model: 'openrouter/free' }} models={models} update={update} />);
    await screen.findByRole('option', { name: 'fixture/model:free' });
    expect(call).toHaveBeenCalledWith('models', { provider_id: 'openrouter' });
    await userEvent.selectOptions(screen.getByLabelText('Model'), 'fixture/model:free');
    expect(update).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ provider_id: 'openrouter', model: 'fixture/model:free' }));
    expect(call.mock.calls.some(([action]) => action === 'settings')).toBe(false);
  });
});
