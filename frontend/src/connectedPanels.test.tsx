import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import App from './App';
import { BrowserPanel } from './browserPanel';
import { GitHubSettings } from './githubSettings';
import { DEFAULT_SETTINGS } from './types';

const project = { id: 'project_fixture', name: 'Fixture project', path: '/fixture/project' };
function bridge(responses: Record<string, unknown>) {
  const call = vi.fn(async (action: string, data: Record<string, unknown> = {}) => {
    if (!(action in responses)) throw new Error(`Unexpected action ${action}`);
    const response = responses[action]; return typeof response === 'function' ? response(data) : response;
  });
  window.pywebview = { api: { call, mode: vi.fn(async () => ({ ok: true })) } };
  return call;
}

class ResizeFixture {
  static instances: ResizeFixture[] = [];
  observe = vi.fn(); unobserve = vi.fn(); disconnect = vi.fn();
  constructor(public callback: () => void) { ResizeFixture.instances.push(this); }
}
function browserEnvironment() {
  ResizeFixture.instances = [];
  vi.stubGlobal('ResizeObserver', ResizeFixture);
  vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => window.setTimeout(() => callback(performance.now()), 1));
  vi.stubGlobal('cancelAnimationFrame', (id: number) => window.clearTimeout(id));
  const rect = { x: 910, y: 92, width: 480, height: 610, top: 92, left: 910, right: 1390, bottom: 702, toJSON: () => ({}) };
  vi.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function (this: Element) {
    return this.id === 'forge-browser-panel' ? rect as DOMRect : { ...rect, width: 0, height: 0 } as DOMRect;
  });
  return rect;
}
const browserState = { running: true, visible: true, available: true, url: 'https://fixture.invalid/', title: 'Fixture browser' };
afterEach(() => { vi.unstubAllGlobals(); });

describe('GitHub connection and project scope', () => {
  it('keeps credentials out of bootstrap, persistent browser storage and unrelated API calls', async () => {
    const responses: Record<string, unknown> = { github_status: { connected: false, gh_available: true }, projects: { projects: [project] },
      github_connect: () => { responses.github_status = { connected: true, login: 'fixture-owner', repositories: [] }; return { connected: true }; } };
    const call = bridge(responses);
    const storage = vi.spyOn(Storage.prototype, 'setItem');
    render(<GitHubSettings notify={vi.fn()} />);
    const token = await screen.findByLabelText('GitHub token') as HTMLInputElement;
    expect(token.type).toBe('password');
    expect(call.mock.calls.map(([action]) => action).sort()).toEqual(['github_status', 'projects']);
    await userEvent.type(token, 'synthetic_github_fixture_token');
    await userEvent.click(screen.getByRole('button', { name: 'Connect' }));
    await screen.findByText('fixture-owner');
    expect(call).toHaveBeenCalledWith('github_connect', { token: 'synthetic_github_fixture_token' });
    expect(token.value).toBe(''); expect(storage).not.toHaveBeenCalled();
    expect(call.mock.calls.filter(([, data]) => JSON.stringify(data).includes('synthetic_github_fixture_token'))).toHaveLength(1);
    expect(call.mock.calls.some(([action]) => ['bootstrap', 'settings', 'start_chat', 'github_create_pr'].includes(action))).toBe(false);
  });

  it('browses an owner, connects only the selected project and confirms each clone', async () => {
    const repo = { full_name: 'fixture-owner/synthetic-repository', description: 'Fixture repository.' };
    const responses: Record<string, unknown> = { github_status: { connected: true, public: true, repositories: [] }, projects: { projects: [project] },
      github_repos: { repositories: [repo], has_more: true }, github_select_repo: (data: Record<string, unknown>) => {
        responses.github_status = { connected: true, public: true, repositories: [{ ...repo, project_id: data.project_id }] }; return { selected: true };
      }, github_clone: { project: { id: 'cloned_fixture', name: 'Synthetic clone', path: '/fixture/managed-clone' } } };
    const call = bridge(responses); const notify = vi.fn();
    render(<GitHubSettings notify={notify} />);
    await screen.findByLabelText('Connect to a Forge project');
    await userEvent.selectOptions(screen.getByLabelText('Connect to a Forge project'), project.id);
    await userEvent.type(screen.getByLabelText('GitHub repository owner'), '  fixture-owner  ');
    await userEvent.click(screen.getByRole('button', { name: 'Browse' }));
    await screen.findByText(repo.full_name);
    expect(call).toHaveBeenCalledWith('github_repos', { page: 1, owner: 'fixture-owner' });
    const row = screen.getByText(repo.full_name).closest('.github-repository') as HTMLElement;
    await userEvent.click(within(row).getByRole('button', { name: 'Connect' }));
    await waitFor(() => expect(call).toHaveBeenCalledWith('github_select_repo', { full_name: repo.full_name, project_id: project.id }));
    await screen.findByRole('heading', { name: 'Selected repositories' });
    await userEvent.click(within(row).getByRole('button', { name: 'Clone' }));
    expect(await screen.findByRole('dialog')).toBeTruthy();
    expect(call.mock.calls.some(([action]) => action === 'github_clone')).toBe(false);
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByRole('dialog')).toBeNull();
    await userEvent.click(within(row).getByRole('button', { name: 'Clone' }));
    await userEvent.click(screen.getByRole('button', { name: 'Clone project' }));
    await waitFor(() => expect(call).toHaveBeenCalledWith('github_clone', { full_name: repo.full_name }));
    expect(call.mock.calls.filter(([action]) => action === 'github_clone')).toHaveLength(1);
    await waitFor(() => expect(notify).toHaveBeenCalledWith('Repository cloned and registered in Projects.'));
    await userEvent.click(screen.getByRole('button', { name: 'Next' }));
    await waitFor(() => expect(call).toHaveBeenCalledWith('github_repos', { page: 2, owner: 'fixture-owner' }));
  });
});

describe('native browser panel lifecycle', () => {
  it('binds the native surface to its measured panel and navigates without opening an external window', async () => {
    const rect = browserEnvironment();
    const call = bridge({ browser_native_status: browserState, browser_native_show: { visible: true }, browser_native_hide: { visible: false },
      browser_native_navigate: { ...browserState, url: 'https://example.invalid/research' }, browser_native_back: browserState, browser_native_forward: browserState, browser_native_reload: browserState });
    const view = render(<BrowserPanel />);
    await waitFor(() => expect(call).toHaveBeenCalledWith('browser_native_show', { panel_id: 'forge-browser-panel', rect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height }, viewport: { width: innerWidth, height: innerHeight } }));
    const address = screen.getByRole('textbox', { name: 'Agent browser address' });
    await userEvent.clear(address); await userEvent.type(address, 'example.invalid/research{Enter}');
    await waitFor(() => expect(call).toHaveBeenCalledWith('browser_native_navigate', { url: 'https://example.invalid/research' }));
    await userEvent.click(screen.getByRole('button', { name: 'Browser back' }));
    await userEvent.click(screen.getByRole('button', { name: 'Browser forward' }));
    await userEvent.click(screen.getByRole('button', { name: 'Reload browser' }));
    expect(call).toHaveBeenCalledWith('browser_native_back', {});
    expect(call).toHaveBeenCalledWith('browser_native_forward', {});
    expect(call).toHaveBeenCalledWith('browser_native_reload', {});
    expect(call.mock.calls.some(([action]) => action === 'browser_native_open')).toBe(false);
    view.unmount();
    expect(call).toHaveBeenCalledWith('browser_native_hide', {});
    expect(ResizeFixture.instances.every(instance => instance.disconnect.mock.calls.length === 1)).toBe(true);
  });

  it('hides behind an existing dialog when its open attribute changes and rebinds after it closes', async () => {
    browserEnvironment();
    const call = bridge({ browser_native_status: browserState, browser_native_show: { visible: true }, browser_native_hide: { visible: false } });
    const view = render(<><dialog data-testid="existing-dialog">Existing dialog</dialog><BrowserPanel /></>);
    await waitFor(() => expect(call).toHaveBeenCalledWith('browser_native_show', expect.anything()));
    const dialog = screen.getByTestId('existing-dialog');
    await act(async () => { dialog.setAttribute('open', ''); });
    await waitFor(() => expect(call).toHaveBeenCalledWith('browser_native_hide', {}));
    const shown = call.mock.calls.filter(([action]) => action === 'browser_native_show').length;
    await act(async () => { dialog.removeAttribute('open'); });
    await waitFor(() => expect(call.mock.calls.filter(([action]) => action === 'browser_native_show').length).toBe(shown + 1));
    view.unmount();
  });

  it('hides again when a delayed bind completes after disposal', async () => {
    browserEnvironment(); let resolveShow: (value: unknown) => void = () => {};
    const shown = new Promise(resolve => { resolveShow = resolve; });
    const call = bridge({ browser_native_status: browserState, browser_native_show: () => shown, browser_native_hide: { visible: false } });
    const view = render(<BrowserPanel />);
    await waitFor(() => expect(call).toHaveBeenCalledWith('browser_native_show', expect.anything()));
    view.unmount();
    expect(call.mock.calls.filter(([action]) => action === 'browser_native_hide')).toHaveLength(1);
    await act(async () => { resolveShow({ visible: true }); await shown; });
    await waitFor(() => expect(call.mock.calls.filter(([action]) => action === 'browser_native_hide')).toHaveLength(2));
  });

  it('keeps a non-native browser client in its documented desktop fallback', () => {
    delete window.pywebview; const fetch = vi.spyOn(window, 'fetch');
    render(<BrowserPanel />);
    expect(screen.getByRole('heading', { name: 'Open Forge desktop' })).toBeTruthy();
    expect(fetch).not.toHaveBeenCalled();
  });

  it('opens from the desktop event and switches to project-scoped files in the app', async () => {
    browserEnvironment();
    const settings = { ...DEFAULT_SETTINGS, model: 'fixture', context: 32768 };
    const call = bridge({ bootstrap: { settings, projects: [project], chats: [], runs: [], capabilities: {} }, models: { models: [{ name: 'fixture' }] },
      projects: { projects: [project] }, chats: { chats: [] }, setup_status: { first_run: false, completed: true, jobs: [], state: {}, catalog: [] },
      browser_native_status: browserState, browser_native_show: { visible: true }, browser_native_hide: { visible: false },
      workspace_files: { entries: [{ name: 'fixture.txt', path: 'fixture.txt', is_dir: false }] }, workspace_read: { content: 'Synthetic file preview.' } });
    const view = render(<App />);
    await act(async () => { window.dispatchEvent(new Event('pywebviewready')); });
    await userEvent.click(await screen.findByRole('button', { name: 'Fixture project' }));
    await act(async () => { window.dispatchEvent(new Event('forge:browser-open')); });
    await screen.findByRole('textbox', { name: 'Agent browser address' });
    await userEvent.click(screen.getByRole('button', { name: 'Files' }));
    await screen.findByRole('button', { name: 'fixture.txt' });
    expect(call).toHaveBeenCalledWith('workspace_files', { project_id: project.id, path: '.' });
    expect(call).toHaveBeenCalledWith('browser_native_hide', {});
    view.unmount();
  });
});
