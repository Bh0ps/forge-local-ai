import { useState } from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { EngineEditor, SettingsPage } from '../settings';
import { DEFAULT_SETTINGS, type Settings } from '../types';

vi.mock('../browserSettings', () => ({ BrowserSettings: () => <h2>Connected browser tabs</h2>, PairBrowserSettings: () => <button>Pair another browser</button> }));
vi.mock('../dictationSettings', () => ({ DictationSettings: () => <h2>Voice input settings</h2> }));
vi.mock('../openRouterSettings', () => ({ OpenRouterSettings: () => <h2>OpenRouter helper settings</h2> }));

function bridge(responses: Record<string, unknown | ((data: Record<string, unknown>) => unknown)> = {}) {
  const call = vi.fn(async (action: string, data: Record<string, unknown> = {}) => {
    if (action === 'providers' && !(action in responses)) return { providers: [] };
    const result = responses[action];
    if (result === undefined) throw new Error(`Unexpected action: ${action}`);
    return typeof result === 'function' ? result(data) : result;
  });
  window.pywebview = { api: { call } };
  return call;
}
function workspace(settings: Settings = DEFAULT_SETTINGS, initialTab = 'general') {
  const onChange = vi.fn(async () => {}), selected = vi.fn();
  function Harness() {
    const [tab, setTab] = useState(initialTab);
    return <SettingsPage settings={settings} onChange={onChange} tab={tab} setTab={value => { selected(value); setTab(value); }} notify={vi.fn()} capabilities={{}} models={[]} refreshModels={async () => {}} applyRecommendation={async () => {}} />;
  }
  render(<Harness />); return { onChange, selected };
}
const provider = { id: '', name: 'Studio engine', type: 'openai', base_url: 'http://127.0.0.1:1234/v1', enabled: true };

describe('settings navigation', () => {
  beforeEach(() => { bridge(); });
  it('groups all existing sections and preserves deep navigation identities', async () => {
    const { selected, onChange } = workspace(DEFAULT_SETTINGS, 'dictation');
    const nav = screen.getByRole('navigation', { name: 'Settings sections' });
    expect(within(nav).getAllByRole('button')).toHaveLength(13);
    expect(screen.getByRole('heading', { name: 'Voice input settings' })).toBeTruthy();
    expect(within(nav).getByRole('button', { name: 'Dictation' }).getAttribute('aria-current')).toBe('page');
    expect(within(nav).getByText('Connections')).toBeTruthy();
    await userEvent.click(within(nav).getByRole('button', { name: 'Models & engines' }));
    expect(selected).toHaveBeenLastCalledWith('models');
    expect(await screen.findByRole('combobox', { name: 'Active engine' })).toBeTruthy();
    expect(onChange).not.toHaveBeenCalled();
  });
  it('finds specific controls by keyword and reveals advanced controls without changing their values', async () => {
    const settings = { ...DEFAULT_SETTINGS, temperature: 0.4, goal_limits: { minutes: 83, tokens: 17000, rounds: 41, tools: 19 } };
    const { onChange } = workspace(settings);
    const advanced = screen.getByText('Advanced conversation settings').closest('details')!;
    expect(advanced.open).toBe(false);
    await userEvent.type(screen.getByRole('textbox', { name: 'Find a setting' }), 'temperature');
    const nav = screen.getByRole('navigation', { name: 'Settings sections' });
    expect(within(nav).getAllByRole('button').map(button => button.getAttribute('aria-label') || button.textContent)).toEqual(['Clear settings search', 'General']);
    await waitFor(() => expect(advanced.open).toBe(true));
    expect((screen.getByRole('slider', { name: 'Temperature' }) as HTMLInputElement).value).toBe('0.4');
    expect((screen.getByRole('spinbutton', { name: 'Time limit (minutes)' }) as HTMLInputElement).value).toBe('83');
    expect(onChange).not.toHaveBeenCalled();
  });
  it('provides a clear search result and restores keyboard focus when cleared', async () => {
    workspace(); const input = screen.getByRole('textbox', { name: 'Find a setting' });
    await userEvent.type(input, 'unmatched phrase');
    expect(within(screen.getByRole('navigation')).getByRole('status').textContent).toContain('No matching settings');
    await userEvent.click(screen.getByRole('button', { name: 'Clear settings search' }));
    expect(document.activeElement).toBe(input);
    expect((input as HTMLInputElement).value).toBe('');
    expect(within(screen.getByRole('navigation')).getAllByRole('button')).toHaveLength(13);
  });
  it('keeps browser pairing with browser connections and retains normal conversation controls', async () => {
    workspace();
    expect(screen.queryByRole('button', { name: 'Pair another browser' })).toBeNull();
    expect(screen.getByRole('slider', { name: /Context window/ })).toBeTruthy();
    expect(screen.getByRole('switch', { name: 'Model reasoning' })).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: /^Browser$/ }));
    expect(screen.getByRole('button', { name: 'Pair another browser' })).toBeTruthy();
  });
  it('uses the same section identities in compact navigation and retains the current section during search', async () => {
    const { selected } = workspace();
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Settings section' }), 'shortcuts');
    expect(selected).toHaveBeenLastCalledWith('shortcuts');
    expect(screen.getByRole('heading', { name: 'Keyboard shortcuts' })).toBeTruthy();
    await userEvent.type(screen.getByRole('textbox', { name: 'Find a setting' }), 'API key');
    const picker = screen.getByRole('combobox', { name: 'Settings section' });
    expect(within(picker).getAllByRole('option').map(option => option.getAttribute('value'))).toEqual(['shortcuts', 'models', 'openrouter']);
    await userEvent.selectOptions(picker, 'models');
    expect(selected).toHaveBeenLastCalledWith('models');
    expect(await screen.findByRole('combobox', { name: 'Active engine' })).toBeTruthy();
  });
  it('preserves other goal limits and refuses fractional limits', async () => {
    const { onChange } = workspace({ ...DEFAULT_SETTINGS, goal_limits: { minutes: 83, tokens: 17000, rounds: 41, tools: 19 } });
    await userEvent.click(screen.getByText('Advanced conversation settings'));
    const input = screen.getByLabelText('Time limit (minutes)');
    fireEvent.change(input, { target: { value: '3.5' } }); expect(onChange).not.toHaveBeenCalled();
    fireEvent.change(input, { target: { value: '90' } });
    expect(onChange).toHaveBeenCalledWith({ goal_limits: { minutes: 90, tokens: 17000, rounds: 41, tools: 19 } });
  });
  it('adds a separate endpoint for built-in Ollama without changing the active engine', async () => {
    const call = bridge({ providers: { providers: [{ id: 'ollama', name: 'Ollama', kind: 'ollama', url: 'http://127.0.0.1:11434/api' }] }, provider_test: { ok: true, model_count: 1 }, provider_save: { id: 'new-endpoint' } });
    const { onChange } = workspace(DEFAULT_SETTINGS, 'models');
    await userEvent.click(await screen.findByRole('button', { name: 'Add endpoint' }));
    expect(screen.getByRole('heading', { name: 'Add local engine' })).toBeTruthy();
    expect(screen.getByText(/default Ollama connection stays available/)).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: 'Test connection' }));
    expect(call).toHaveBeenCalledWith('provider_test', { draft: { kind: 'ollama', url: 'http://127.0.0.1:11434/api' }, id: undefined, api_key: undefined });
    expect(call.mock.calls.some(([action]) => action === 'provider_save')).toBe(false);
    await userEvent.click(screen.getByRole('button', { name: 'Save engine' }));
    await waitFor(() => expect(call).toHaveBeenCalledWith('provider_save', { kind: 'ollama', url: 'http://127.0.0.1:11434/api', name: 'Ollama endpoint', id: undefined, api_key: undefined, enabled: true }));
    expect(onChange).not.toHaveBeenCalled();
  });
  it('opens dedicated OpenRouter settings without converting or resaving a cloud profile', async () => {
    const call = bridge({ providers: { providers: [{ id: 'openrouter', name: 'OpenRouter', kind: 'openrouter', url: 'https://openrouter.ai/api/v1' }] } });
    const { selected, onChange } = workspace(DEFAULT_SETTINGS, 'models');
    await userEvent.click(await screen.findByRole('button', { name: 'Configure' }));
    expect(selected).toHaveBeenLastCalledWith('openrouter');
    expect(await screen.findByRole('heading', { name: 'OpenRouter helper settings' })).toBeTruthy();
    expect(screen.queryByRole('heading', { name: 'Configure engine' })).toBeNull();
    expect(call.mock.calls.some(([action]) => ['provider_save', 'provider_test'].includes(action))).toBe(false);
    expect(onChange).not.toHaveBeenCalled();
  });
});

describe('local engine connection editor', () => {
  it('tests a draft once without saving, while preventing overlapping tests and saves', async () => {
    let finish!: (value: unknown) => void;
    const call = bridge({ provider_test: () => new Promise(resolve => { finish = resolve; }) });
    render(<EngineEditor provider={provider} close={vi.fn()} saved={vi.fn(async () => {})} />);
    const test = screen.getByRole('button', { name: 'Test connection' });
    fireEvent.click(test); fireEvent.click(test);
    fireEvent.submit(test.closest('form')!);
    expect(call.mock.calls.filter(([action]) => action === 'provider_test' || action === 'provider_save')).toHaveLength(1);
    expect(call).toHaveBeenCalledWith('provider_test', { draft: { kind: 'openai-compatible', url: provider.base_url }, id: undefined, api_key: undefined });
    expect((screen.getByRole('button', { name: 'Save engine' }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByLabelText('Base URL') as HTMLInputElement).disabled).toBe(true);
    await act(async () => finish({ ok: true, model_count: 3 }));
    expect(screen.getByRole('status').textContent).toContain('Connection verified');
    expect(screen.getByRole('status').textContent).toContain('3 models are available');
    expect(screen.getByRole('heading', { name: 'Add local engine' })).toBeTruthy();
    expect(call.mock.calls.some(([action]) => action === 'provider_save')).toBe(false);
  });
  it('explains an empty catalog honestly and does not print a raw response', async () => {
    bridge({ provider_test: { ok: true, model_count: 0, private_opaque_metadata: 'not-for-display' } });
    render(<EngineEditor provider={provider} close={vi.fn()} saved={vi.fn(async () => {})} />);
    await userEvent.click(screen.getByRole('button', { name: 'Test connection' }));
    expect((await screen.findByRole('status')).textContent).toContain('does not advertise any models');
    expect(screen.queryByText('not-for-display')).toBeNull();
  });
  it('does not claim success when the endpoint provides no usable test result', async () => {
    bridge({ provider_test: { ok: true, model_count: -1 } });
    render(<EngineEditor provider={provider} close={vi.fn()} saved={vi.fn(async () => {})} />);
    await userEvent.click(screen.getByRole('button', { name: 'Test connection' }));
    expect((await screen.findByRole('alert')).textContent).toContain('Check its API compatibility');
    expect(screen.queryByText('Connection verified')).toBeNull();
  });
  it('ignores a cancelled test result when another editor opens', async () => {
    let reject!: (error: Error) => void;
    bridge({ provider_test: () => new Promise((_resolve, fail) => { reject = fail; }) });
    function Harness() {
      const [open, setOpen] = useState(true);
      return open ? <EngineEditor provider={provider} close={() => setOpen(false)} saved={vi.fn(async () => {})} /> : <button onClick={() => setOpen(true)}>Add another engine</button>;
    }
    render(<Harness />); await userEvent.click(screen.getByRole('button', { name: 'Test connection' }));
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    await userEvent.click(screen.getByRole('button', { name: 'Add another engine' }));
    await act(async () => reject(new Error('Old connection failure')));
    expect(screen.queryByRole('alert')).toBeNull();
    expect((screen.getByRole('button', { name: 'Test connection' }) as HTMLButtonElement).disabled).toBe(false);
  });
  it('clears typed keys when the target or provider changes and on cancel', async () => {
    const call = bridge({ provider_test: { ok: true, model_count: 1 } });
    const close = vi.fn(); render(<EngineEditor provider={{ ...provider, id: 'saved-engine' }} close={close} saved={vi.fn(async () => {})} />);
    const password = screen.getByLabelText('API key (optional)') as HTMLInputElement;
    await userEvent.type(password, 'synthetic-only');
    fireEvent.change(screen.getByLabelText('Base URL'), { target: { value: 'http://127.0.0.1:8080/v1' } });
    expect(password.value).toBe('');
    await userEvent.type(password, 'another-synthetic-key');
    await userEvent.selectOptions(screen.getByLabelText('API protocol'), 'ollama');
    expect(password.value).toBe('');
    await userEvent.click(screen.getByRole('button', { name: 'Test connection' }));
    expect(call).toHaveBeenCalledWith('provider_test', expect.objectContaining({ id: 'saved-engine', api_key: undefined }));
    await userEvent.type(password, 'third-synthetic-key');
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(password.value).toBe(''); expect(close).toHaveBeenCalledOnce();
  });
  it('saves only editable fields once and leaves trusted provider metadata to the coordinator', async () => {
    let finish!: (value: unknown) => void;
    const call = bridge({ provider_save: () => new Promise(resolve => { finish = resolve; }) });
    const saved = vi.fn(async () => {});
    render(<EngineEditor provider={{ ...provider, id: 'existing', context_limit: 65536, capabilities: ['tools'] }} close={vi.fn()} saved={saved} />);
    await userEvent.type(screen.getByLabelText('API key (optional)'), 'synthetic-only');
    const save = screen.getByRole('button', { name: 'Save engine' });
    fireEvent.submit(save.closest('form')!); fireEvent.submit(save.closest('form')!);
    expect(call.mock.calls.filter(([action]) => action === 'provider_save')).toHaveLength(1);
    expect(call).toHaveBeenCalledWith('provider_save', { id: 'existing', name: provider.name, kind: 'openai-compatible', url: provider.base_url, api_key: 'synthetic-only' });
    await act(async () => finish({ id: 'existing' }));
    expect(saved).toHaveBeenCalledOnce();
    expect((screen.getByLabelText('API key (optional)') as HTMLInputElement).value).toBe('');
  });
  it('validates URLs before network requests and permits correction after failure', async () => {
    const call = bridge({ provider_test: { ok: true, model_count: 1 } });
    render(<EngineEditor provider={provider} close={vi.fn()} saved={vi.fn(async () => {})} />);
    fireEvent.change(screen.getByLabelText('Base URL'), { target: { value: 'http://name:secret@127.0.0.1:1234/v1' } });
    await userEvent.click(screen.getByRole('button', { name: 'Test connection' }));
    expect(call.mock.calls.some(([action]) => action === 'provider_test' || action === 'provider_save')).toBe(false); expect(screen.getByRole('alert').textContent).toContain('without embedded credentials');
    fireEvent.change(screen.getByLabelText('Base URL'), { target: { value: provider.base_url } });
    expect(screen.queryByRole('alert')).toBeNull();
    await userEvent.click(screen.getByRole('button', { name: 'Test connection' }));
    expect((await screen.findByRole('status')).textContent).toContain('1 model is available');
  });
});
