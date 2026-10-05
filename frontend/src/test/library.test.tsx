import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { filterLibraryEntries, PluginsPage, type LibraryEntry, type LibrarySkill } from '../pluginsPage';

const starter: LibraryEntry = { id: 'starter:research', name: 'Research', description: 'Research sources and cite evidence.', kind: 'skill', category: 'research', source: 'builtin:skill/research', catalog_name: 'Forge starters', license: 'MIT', builtin: true, installed: true, enabled: true, automatic: false, skill_id: 'skill-1', tags: ['browser', 'citations'] };
const plugin: LibraryEntry = { id: 'external:docs', name: 'Docs tools', description: 'Tool connections for documentation.', kind: 'plugin', category: 'documentation', source: 'https://github.com/example/tools/tree/fixture/docs', catalog_name: 'Supported catalog', license: 'Review license', compatibility: 'Inspection required', setup: 'Configure connections after import.' };
function bridge(overrides: Record<string, unknown | ((data: Record<string, unknown>) => unknown)> = {}) {
  const skill: LibrarySkill = { id: 'skill-1', name: 'Research', description: starter.description, category: 'research', enabled: true, automatic: false, builtin: true, library_id: starter.id };
  const responses: Record<string, unknown> = { catalog_discover: { entries: [starter, plugin], sources: [{ id: 'forge', name: 'Forge starters', builtin: true, state: 'ready', count: 1 }] }, skills: { skills: [skill] }, plugins: { plugins: [] }, skill_preview: { text: '# Research instructions\nRead the sources before reaching a conclusion.' }, ...overrides };
  const call = vi.fn(async (action: string, data: Record<string, unknown> = {}) => { if (!(action in responses)) throw new Error(`Unexpected action: ${action}`); const value = responses[action]; return typeof value === 'function' ? value(data) : value; });
  window.pywebview = { api: { call } }; return { call, responses, skill };
}
describe('setup library', () => {
  it('opens Discover and searches all supported metadata automatically without installing anything', async () => {
    const { call } = bridge(); render(<PluginsPage notify={vi.fn()} projectId="project-1" />);
    await screen.findByRole('heading', { name: 'Research' }); expect(call).toHaveBeenCalledWith('catalog_discover', { project_id: 'project-1' });
    expect(screen.getByRole('button', { name: 'Discover' }).getAttribute('aria-pressed')).toBe('true');
    await userEvent.type(screen.getByRole('textbox', { name: 'Search library' }), 'browser citations');
    expect(screen.getByRole('heading', { name: 'Research' })).toBeTruthy(); expect(screen.queryByRole('heading', { name: 'Docs tools' })).toBeNull();
    expect(call.mock.calls.filter(([action]) => action === 'catalog_discover')).toHaveLength(1);
    expect(call.mock.calls.some(([action]) => action === 'plugin_install' || action === 'skill_install')).toBe(false);
  });
  it('combines multiword search, types and categories, and includes MCP in plugins', () => {
    const mcp = { ...plugin, id: 'mcp', kind: 'mcp' }; const entries = [starter, plugin, mcp];
    expect(filterLibraryEntries(entries, 'for tools', 'plugin', 'documentation').map(entry => entry.id)).toEqual([plugin.id, 'mcp']);
    expect(filterLibraryEntries(entries, 'research browser', 'skill', 'research')).toEqual([starter]);
    expect(filterLibraryEntries(entries, 'unrelated', 'all', '')).toEqual([]);
  });
  it('lets enabled skills turn off without changing an explicitly disabled automatic selection', async () => {
    const { call, responses, skill } = bridge({ skill_toggle: (data: Record<string, unknown>) => { responses.skills = { skills: [{ ...skill, enabled: data.enabled, automatic: data.automatic }] }; return { ok: true }; } });
    render(<PluginsPage notify={vi.fn()} projectId="project-1" />); await screen.findByRole('heading', { name: 'Research' });
    await userEvent.click(screen.getByRole('switch', { name: 'Enable Research' }));
    await waitFor(() => expect(call).toHaveBeenCalledWith('skill_toggle', { id: 'skill-1', enabled: false, automatic: false, project_id: 'project-1' }));
    await waitFor(() => expect(screen.getByRole('switch', { name: 'Enable Research' }).getAttribute('aria-checked')).toBe('false'));
    await userEvent.click(screen.getByRole('button', { name: /Installed/ }));
    expect((screen.getByRole('switch', { name: 'Automatically use Research' }) as HTMLButtonElement).disabled).toBe(true);
  });
  it('previews a disabled skill without enabling it or granting an action', async () => {
    const { call } = bridge({ skills: { skills: [{ id: 'skill-1', name: 'Research', enabled: false, automatic: false, builtin: true }] } });
    render(<PluginsPage notify={vi.fn()} />); await screen.findByRole('heading', { name: 'Research' }); await userEvent.click(screen.getByRole('button', { name: 'View skill' }));
    expect(await screen.findByText('Read the sources before reaching a conclusion.')).toBeTruthy();
    expect(call).toHaveBeenCalledWith('skill_preview', { id: 'skill-1', project_id: undefined });
    expect(screen.getByRole('switch', { name: 'Enable Research in details' }).getAttribute('aria-checked')).toBe('false');
    expect(call.mock.calls.some(([action]) => action === 'skill_toggle' || action === 'plugin_install')).toBe(false);
  });
  it('reviews compatibility and staged source before a discovered plugin can install', async () => {
    const { call } = bridge({ plugin_inspect: { name: 'Docs tools', compatible: true, inspection_id: 'staged-docs', compatibility: 'Portable skills', warnings: ['Connections need setup.'], licenses: ['LICENSE'] }, plugin_install: { ok: true } });
    render(<PluginsPage notify={vi.fn()} />); await screen.findByRole('heading', { name: 'Docs tools' }); await userEvent.click(screen.getByRole('button', { name: 'Review & install' }));
    expect((screen.getByLabelText('Source') as HTMLInputElement).value).toBe(plugin.source); expect(call.mock.calls.some(([action]) => action === 'plugin_install')).toBe(false);
    await userEvent.click(screen.getByRole('button', { name: 'Inspect source' })); await screen.findByText('Portable skills'); await userEvent.click(screen.getByRole('button', { name: 'Install reviewed plugin' }));
    await waitFor(() => expect(call).toHaveBeenCalledWith('plugin_install', { inspection_id: 'staged-docs', source: plugin.source, reviewed: true }));
  });
  it('preserves cached starters when a linked source fails and offers source retry', async () => {
    const { call } = bridge({ catalog_discover: { entries: [starter], sources: [{ id: 'forge', name: 'Forge starters', builtin: true, state: 'ready' }, { id: 'remote', name: 'Remote catalog', error: 'Offline', state: 'error' }] } });
    render(<PluginsPage notify={vi.fn()} />); await screen.findByRole('heading', { name: 'Research' }); await userEvent.click(screen.getByRole('button', { name: 'View sources' }));
    const dialog = screen.getByRole('dialog'); expect(within(dialog).getByText('Remote catalog')).toBeTruthy(); expect(within(dialog).getByText('Offline')).toBeTruthy();
    expect(screen.queryByText('Reviewed')).toBeNull(); await userEvent.click(screen.getByRole('button', { name: 'Close dialog' })); await userEvent.click(screen.getByRole('button', { name: 'Refresh library' }));
    await waitFor(() => expect(call).toHaveBeenCalledWith('catalog_discover', { project_id: undefined, refresh: true }));
  });
  it('shows a failed discovery with retry and recovers without resetting search', async () => {
    const { call, responses } = bridge({ catalog_discover: () => { throw new Error('Catalog unavailable'); } });
    render(<PluginsPage notify={vi.fn()} />); await screen.findByRole('alert'); await userEvent.type(screen.getByRole('textbox', { name: 'Search library' }), 'research');
    responses.catalog_discover = { entries: [starter], sources: [] }; await userEvent.click(screen.getByRole('button', { name: 'Retry' })); await screen.findByRole('heading', { name: 'Research' });
    expect((screen.getByRole('textbox', { name: 'Search library' }) as HTMLInputElement).value).toBe('research'); expect(call).toHaveBeenCalledWith('catalog_discover', { project_id: undefined, refresh: true });
  });
  it('ignores an old project discovery response after switching projects', async () => {
    let resolveOld: (value: unknown) => void = () => {};
    bridge({ catalog_discover: (data: Record<string, unknown>) => data.project_id === 'old' ? new Promise(resolve => { resolveOld = resolve; }) : { entries: [plugin], sources: [] } });
    const view = render(<PluginsPage notify={vi.fn()} projectId="old" />); view.rerender(<PluginsPage notify={vi.fn()} projectId="new" />); await screen.findByRole('heading', { name: 'Docs tools' });
    await act(async () => resolveOld({ entries: [starter], sources: [] })); expect(screen.getByRole('heading', { name: 'Docs tools' })).toBeTruthy(); expect(screen.queryByRole('heading', { name: 'Research' })).toBeNull();
  });
  it('keeps plugin details separate from skills belonging to the same plugin', async () => {
    const { call } = bridge({ skills: { skills: [{ id: 'child-skill', name: 'Child skill', plugin_id: 'plugin-1', enabled: true, automatic: false }] }, plugins: { plugins: [{ id: 'plugin-1', name: 'Parent plugin', source: 'builtin:research', enabled: true }] } });
    render(<PluginsPage notify={vi.fn()} />); await screen.findByRole('heading', { name: 'Research' }); await userEvent.click(screen.getByRole('button', { name: /Installed/ }));
    const card = screen.getByRole('heading', { name: 'Parent plugin' }).closest('article')!; await userEvent.click(within(card).getByRole('button', { name: 'View details' }));
    const dialog = screen.getByRole('dialog'); expect(within(dialog).getByRole('heading', { name: 'Parent plugin' })).toBeTruthy(); expect(within(dialog).getByRole('button', { name: 'Review update' })).toBeTruthy();
    expect(call.mock.calls.some(([action]) => action === 'skill_preview')).toBe(false);
  });
});
