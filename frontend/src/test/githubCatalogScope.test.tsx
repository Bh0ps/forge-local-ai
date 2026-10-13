import { act, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { GitHubSettings } from '../githubSettings';

function deferred() { let resolve!: (value: unknown) => void, reject!: (error: Error) => void; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; }
async function workspace(browse: (data: Record<string, unknown>) => unknown) {
  const call = vi.fn(async (action: string, data: Record<string, unknown> = {}) => {
    if (action === 'github_status') return { connected: true, public: true };
    if (action === 'projects') return { projects: [] };
    if (action === 'github_repos') return browse(data);
    if (action === 'github_connect') return { ok: true };
    throw new Error('Unexpected synthetic action');
  });
  window.pywebview = { api: { call } };
  render(<GitHubSettings notify={vi.fn()} />);
  const owner = await screen.findByRole('textbox', { name: 'GitHub repository owner' });
  return { call, owner };
}
const repository = (full_name: string, has_more = true) => ({ repositories: [{ full_name }], has_more });

describe('GitHub repository catalog ownership', () => {
  it('ignores a pending response after its owner changes and starts the new search on page one', async () => {
    const old = deferred(); const pages: Record<string, unknown>[] = [];
    const { owner, call } = await workspace(data => { pages.push(data); return pages.length === 1 ? old.promise : repository('fixture-b/first-page', false); });
    await userEvent.type(owner, 'fixture-a'); await userEvent.click(screen.getByRole('button', { name: 'Browse' }));
    await userEvent.clear(owner); await userEvent.type(owner, 'fixture-b');
    await act(async () => old.resolve(repository('fixture-a/stale-page')));
    expect(screen.queryByText('fixture-a/stale-page')).toBeNull(); expect(screen.queryByRole('button', { name: 'Next' })).toBeNull();
    await userEvent.click(screen.getByRole('button', { name: 'Browse' }));
    await screen.findByText('fixture-b/first-page');
    expect(pages).toEqual([{ owner: 'fixture-a', page: 1 }, { owner: 'fixture-b', page: 1 }]);
    expect(call.mock.calls.some(([action]) => !['github_status', 'projects', 'github_repos'].includes(action))).toBe(false);
  });

  it('clears an existing catalog on owner edits without browsing automatically or skipping pages', async () => {
    const pages: Record<string, unknown>[] = [];
    const { owner } = await workspace(data => { pages.push(data); return repository(String(data.owner || 'default') + '/page-' + data.page); });
    await userEvent.type(owner, 'fixture-a'); await userEvent.click(screen.getByRole('button', { name: 'Browse' }));
    await screen.findByText('fixture-a/page-1');
    await userEvent.click(screen.getByRole('button', { name: 'Next' })); await screen.findByText('fixture-a/page-2');
    await userEvent.clear(owner);
    expect(screen.queryByText('fixture-a/page-2')).toBeNull(); expect(screen.queryByRole('button', { name: 'Next' })).toBeNull(); expect(pages).toHaveLength(2);
    await userEvent.click(screen.getByRole('button', { name: 'Browse' })); await screen.findByText('default/page-1');
    expect(pages[2]).toEqual({ page: 1 });
  });

  it('retains the same submitted owner and consecutive pages through harmless whitespace edits', async () => {
    const pages: Record<string, unknown>[] = [];
    const { owner } = await workspace(data => { pages.push(data); return repository('fixture-a/page-' + data.page); });
    await userEvent.type(owner, 'fixture-a'); await userEvent.click(screen.getByRole('button', { name: 'Browse' }));
    await screen.findByText('fixture-a/page-1'); await userEvent.type(owner, ' ');
    expect(screen.getByText('fixture-a/page-1')).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: 'Next' })); await screen.findByText('fixture-a/page-2');
    expect(pages).toEqual([{ owner: 'fixture-a', page: 1 }, { owner: 'fixture-a', page: 2 }]);
  });

  it('drops an abandoned owner failure but displays an error from the current search', async () => {
    const old = deferred(); let reads = 0;
    const { owner } = await workspace(() => ++reads === 1 ? old.promise : Promise.reject(new Error('Current synthetic repository search failed.')));
    await userEvent.type(owner, 'fixture-a'); await userEvent.click(screen.getByRole('button', { name: 'Browse' }));
    await userEvent.clear(owner); await userEvent.type(owner, 'fixture-b');
    await act(async () => old.reject(new Error('Abandoned synthetic repository search failed.')));
    expect(screen.queryByText('Abandoned synthetic repository search failed.')).toBeNull();
    await userEvent.click(screen.getByRole('button', { name: 'Browse' }));
    expect((await screen.findByRole('alert')).textContent).toContain('Current synthetic repository search failed.');
  });

  it('rejects a delayed response from a replaced connection transport', async () => {
    const old = deferred(); const { owner } = await workspace(() => old.promise);
    await userEvent.type(owner, 'fixture-a'); await userEvent.click(screen.getByRole('button', { name: 'Browse' }));
    window.pywebview = { api: { call: vi.fn(async () => ({})) } };
    await act(async () => old.resolve(repository('fixture-a/previous-transport')));
    expect(screen.queryByText('fixture-a/previous-transport')).toBeNull(); expect(screen.queryByRole('button', { name: 'Next' })).toBeNull();
    expect((screen.getByRole('button', { name: 'Browse' }) as HTMLButtonElement).disabled).toBe(false);
  });

  it('clears account-bound catalogs when the user explicitly reconnects', async () => {
    const { owner, call } = await workspace(() => repository('fixture-a/previous-account'));
    await userEvent.type(owner, 'fixture-a'); await userEvent.click(screen.getByRole('button', { name: 'Browse' }));
    await screen.findByText('fixture-a/previous-account');
    await userEvent.click(screen.getByRole('button', { name: 'Public browsing' }));
    expect(screen.queryByText('fixture-a/previous-account')).toBeNull(); expect(screen.queryByRole('button', { name: 'Next' })).toBeNull();
    expect(call).toHaveBeenCalledWith('github_connect', { public: true });
  });

  it('retains the newer connection status and projects when an initial refresh arrives late', async () => {
    const old = deferred(); let statuses = 0, projects = 0;
    const call = vi.fn(async (action: string) => {
      if (action === 'github_status') return ++statuses === 1 ? old.promise : { connected: true, public: true };
      if (action === 'projects') return { projects: [{ id: projects++ ? 'current' : 'old', name: projects === 1 ? 'Old fixture project' : 'Current fixture project' }] };
      if (action === 'github_connect') return { ok: true };
      throw new Error('Unexpected synthetic action');
    });
    window.pywebview = { api: { call } }; render(<GitHubSettings notify={vi.fn()} />);
    await userEvent.click(screen.getByRole('button', { name: 'Public browsing' }));
    await screen.findByRole('textbox', { name: 'GitHub repository owner' });
    await act(async () => old.resolve({ connected: false, public: false }));
    expect(screen.getByRole('textbox', { name: 'GitHub repository owner' })).toBeTruthy();
    expect(screen.getByRole('option', { name: 'Current fixture project' })).toBeTruthy();
    expect(screen.queryByRole('option', { name: 'Old fixture project' })).toBeNull();
  });

  it('does not surface an expired initial read failure after a newer successful connection', async () => {
    const old = deferred(); let statuses = 0;
    window.pywebview = { api: { call: vi.fn(async (action: string) => {
      if (action === 'github_status') return ++statuses === 1 ? old.promise : { connected: true, public: true };
      if (action === 'projects') return { projects: [] };
      if (action === 'github_connect') return { ok: true };
      throw new Error('Unexpected synthetic action');
    }) } };
    render(<GitHubSettings notify={vi.fn()} />); await userEvent.click(screen.getByRole('button', { name: 'Public browsing' }));
    await screen.findByRole('textbox', { name: 'GitHub repository owner' });
    await act(async () => old.reject(new Error('Expired synthetic status failure.')));
    expect(screen.queryByRole('alert')).toBeNull(); expect(screen.getByRole('textbox', { name: 'GitHub repository owner' })).toBeTruthy();
  });

  it('does not publish an initial status from a replaced connection transport', async () => {
    const old = deferred(); window.pywebview = { api: { call: vi.fn(async action => action === 'github_status' ? old.promise : { projects: [] }) } };
    render(<GitHubSettings notify={vi.fn()} />);
    window.pywebview = { api: { call: vi.fn(async () => ({})) } };
    await act(async () => old.resolve({ connected: true, login: 'Old fixture account' }));
    expect(screen.queryByText('Old fixture account')).toBeNull(); expect(screen.queryByRole('textbox', { name: 'GitHub repository owner' })).toBeNull();
  });

  it('does not overwrite a reopened settings instance with an unmounted initial response', async () => {
    const old = deferred(); window.pywebview = { api: { call: vi.fn(async action => action === 'github_status' ? old.promise : { projects: [] }) } };
    const first = render(<GitHubSettings notify={vi.fn()} />); first.unmount();
    window.pywebview = { api: { call: vi.fn(async action => action === 'github_status' ? { connected: true, public: true } : { projects: [] }) } };
    render(<GitHubSettings notify={vi.fn()} />); await screen.findByRole('textbox', { name: 'GitHub repository owner' });
    await act(async () => old.resolve({ connected: false }));
    expect(screen.getByRole('textbox', { name: 'GitHub repository owner' })).toBeTruthy();
  });
});
