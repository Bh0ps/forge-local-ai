import { useState } from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { scopeWorkspaceRuns, WorkspaceActivity, workspaceActivityGroups, WorkspacePanel, type WorkspaceActivityScope } from './workspacePanel';
import { TaskContextPanel } from './taskContextPanel';
import type { ActivityLive } from './chatActivity';
import type { ActivityGroup, Run, SavedChat } from './types';
import App from './App';
import { DEFAULT_SETTINGS } from './types';
import { DEFAULT_LAYOUT } from './workspaceLayout';

const chat: SavedChat = { id: 'chat', project_id: 'studio', title: 'Synthetic chat', model: 'fixture', messages: [] };
const scope: WorkspaceActivityScope = { page: 'chat', chatId: 'chat', projectId: 'studio', chat, chats: [chat, { ...chat, id: 'other-chat' }, { ...chat, id: 'foreign-chat', project_id: 'other-project' }, { ...chat, id: 'global-chat', project_id: null }] };
const group: ActivityGroup = { run_id: 'saved', status: 'completed', current_step: 'Saved checks passed', tool_count: 2, helper_count: 0, children: [] };
function live(id: string, extra: Partial<Run> = {}): ActivityLive { const run = { id, chat_id: 'chat', project_id: 'studio', status: 'completed', ...extra }; return { run, tools: [], finished: run.status === 'completed' || run.status === 'failed' || run.status === 'paused', status: `${id} activity` }; }
function bridge(overrides: Record<string, unknown> = {}) {
  const call = vi.fn(async (action: string, data: Record<string, unknown>) => {
    const result = overrides[action] ?? { unknown_actions: { actions: [] }, run_activity: { events: [], next_cursor: 0, has_more: false, children: [] }, skills: { skills: [] } }[action as 'unknown_actions' | 'run_activity' | 'skills'];
    if (!result) throw new Error(`Unexpected action: ${action}`);
    return typeof result === 'function' ? result(data) : result;
  });
  window.pywebview = { api: { call } };
  return call;
}

describe('detail pane navigation', () => {
  function Harness() {
    const [tab, setTab] = useState('activity'), [open, setOpen] = useState(true), [width, setWidth] = useState(320);
    return <><button aria-label={open ? 'Close workspace panel' : 'Open workspace panel'} onClick={() => setOpen(!open)}>Details</button>{open && <WorkspacePanel tab={tab} select={setTab} close={() => setOpen(false)} width={width} resize={setWidth}>{tab === 'context' ? <input aria-label="Synthetic search" autoFocus /> : <p>{tab} contents</p>}</WorkspacePanel>}</>;
  }
  it('uses labeled icon tabs, one keyboard stop, arrow wrapping and Home/End without losing focus to panel autofocus', async () => {
    render(<Harness />);
    const tabs = screen.getAllByRole('tab');
    expect(tabs.map(tab => tab.getAttribute('aria-label'))).toEqual(['Context', 'Browser', 'Activity', 'Goals', 'Files']);
    expect(tabs.filter(tab => tab.tabIndex === 0)).toHaveLength(1);
    screen.getByRole('tab', { name: 'Activity' }).focus();
    await userEvent.keyboard('{Home}');
    expect(screen.getByRole('tab', { name: 'Context' }).getAttribute('aria-selected')).toBe('true');
    expect(document.activeElement).toBe(screen.getByRole('tab', { name: 'Context' }));
    expect(screen.getByRole('tabpanel', { name: 'Context' })).toBeTruthy();
    await userEvent.keyboard('{End}{ArrowRight}');
    expect(document.activeElement).toBe(screen.getByRole('tab', { name: 'Context' }));
    await userEvent.keyboard('{ArrowLeft}');
    expect(document.activeElement).toBe(screen.getByRole('tab', { name: 'Files' }));
    await userEvent.click(screen.getByRole('tab', { name: 'Context' }));
    expect(document.activeElement).toBe(screen.getByRole('textbox', { name: 'Synthetic search' }));
  });
  it('retains keyboard resizing and closes through its distinct action, restoring the workspace trigger', async () => {
    render(<Harness />);
    const resize = screen.getByRole('separator', { name: 'Resize workspace panel' });
    expect(resize.getAttribute('aria-valuenow')).toBe('320');
    fireEvent.keyDown(resize, { key: 'ArrowLeft' });
    expect(resize.getAttribute('aria-valuenow')).toBe('330');
    expect(screen.getAllByRole('button', { name: 'Close workspace panel' })).toHaveLength(1);
    await userEvent.click(screen.getByRole('button', { name: 'Close detail pane' }));
    expect(screen.queryByRole('complementary', { name: 'Workspace details' })).toBeNull();
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Open workspace panel' })));
  });
});

describe('workspace activity scope and durable summaries', () => {
  it('wires the real workspace pane to current chat history and persists icon-tab selection', async () => {
    const saved = { ...chat, messages: [{ id: 1, role: 'assistant', content: 'Synthetic saved answer.' }], activity_groups: [group] };
    const rows = [live('chat-run').run, live('other-run', { chat_id: 'other-chat' }).run];
    const call = bridge({ bootstrap: { settings: { ...DEFAULT_SETTINGS, model: 'fixture' }, projects: [{ id: 'studio', name: 'Studio', path: '/synthetic/studio' }], chats: [chat], runs: rows, capabilities: {} },
      get_chat: saved, projects: { projects: [{ id: 'studio', name: 'Studio', path: '/synthetic/studio' }] }, chats: { chats: [chat] }, runs: { runs: rows }, schedules: { schedules: [] }, models: { models: [{ name: 'fixture' }] }, spaces: { spaces: [] }, setup_status: { first_run: false }, draft_get: { revision: 0, content: {} }, draft_save: { revision: 1, content: {} }, host_status: { visible: true, visibility_seq: 0 }, ui_layout_get: DEFAULT_LAYOUT, ui_layout_save: ({ layout }: Record<string, unknown>) => layout });
    render(<App />);
    await act(async () => window.dispatchEvent(new Event('pywebviewready')));
    await screen.findByText('Synthetic saved answer.');
    await userEvent.click(screen.getByRole('button', { name: 'Open workspace panel' }));
    const pane = screen.getByRole('complementary', { name: 'Workspace details' });
    expect(within(pane).getByText('Saved checks passed')).toBeTruthy();
    expect(within(pane).getAllByRole('region', { name: 'Task activity' })).toHaveLength(2);
    expect(within(pane).queryByText('other-run activity')).toBeNull();
    expect(within(pane).queryByText('All quiet')).toBeNull();
    await userEvent.click(within(pane).getByRole('tab', { name: 'Context' }));
    await screen.findByRole('textbox', { name: 'Search skills and notes' });
    await waitFor(() => expect(call).toHaveBeenCalledWith('ui_layout_save', { client_id: 'desktop', layout: { ...DEFAULT_LAYOUT, right_panel_open: true, right_panel_tab: 'context' } }));
  });
  it('includes descendants by ancestor identity while excluding other chats and projects', () => {
    const rows = [live('root'), live('helper', { parent_id: 'root', chat_id: 'helper-chat', project_id: 'helper-worktree' }), live('grandchild', { parent_id: 'helper', chat_id: 'nested-chat' }), live('same-project', { chat_id: 'other-chat' }), live('foreign', { chat_id: 'foreign-chat', project_id: 'other-project' }), live('foreign-helper', { parent_id: 'foreign', chat_id: 'chat', project_id: 'studio' })];
    expect(scopeWorkspaceRuns(rows, scope).map(item => item.run.id)).toEqual(['root', 'helper', 'grandchild']);
    expect(scopeWorkspaceRuns(rows, { ...scope, page: 'builder' }).map(item => item.run.id)).toEqual(['root', 'helper', 'grandchild', 'same-project']);
    const groups = workspaceActivityGroups(rows, scope);
    expect(groups.map(item => item.run_id)).toEqual(['root']);
    expect(groups[0].children[0].run_id).toBe('helper');
    expect(groups[0].children[0].children[0].run_id).toBe('grandchild');
  });
  it('restores saved summaries without live rows and attaches descendants whose saved parent is absent from the live cache', () => {
    const saved = { ...group, children: [{ ...group, run_id: 'saved-helper' }], helper_count: 1 };
    const restored = { ...scope, chat: { ...chat, activity_groups: [saved] } };
    const rows = [live('new-child', { parent_id: 'saved-helper', chat_id: 'helper-private-chat', project_id: 'helper-worktree' })];
    expect(scopeWorkspaceRuns(rows, restored)).toHaveLength(1);
    const groups = workspaceActivityGroups(rows, restored);
    expect(groups).toHaveLength(1);
    expect(groups[0].run_id).toBe('saved');
    expect(groups[0].children[0].children[0].run_id).toBe('new-child');
    bridge();
    render(<WorkspaceActivity lives={[]} scope={restored} notify={() => {}} resume={() => {}} />);
    expect(screen.getByText('Saved checks passed')).toBeTruthy();
    expect(screen.queryByText('No activity yet')).toBeNull();
    expect(screen.queryByText('All quiet')).toBeNull();
  });
  it('fails closed for unknown project ancestry, ignores cyclic records and leaves a new chat empty', () => {
    const global = live('authoritative-global', { chat_id: 'foreign-chat' });
    Object.assign(global.run, { project_id: null });
    const rows = [live('unknown', { project_id: undefined, chat_id: 'unknown-chat' }), live('global', { project_id: undefined, chat_id: 'global-chat' }), global, live('cycle-a', { parent_id: 'cycle-b' }), live('cycle-b', { parent_id: 'cycle-a' })];
    expect(scopeWorkspaceRuns(rows, { ...scope, page: 'builder', projectId: null, chat: null }).map(item => item.run.id)).toEqual(['global', 'authoritative-global']);
    expect(scopeWorkspaceRuns(rows, { ...scope, chatId: null, chat: null })).toEqual([]);
    expect(workspaceActivityGroups([], { ...scope, chatId: 'other-chat' })).toEqual([]);
  });
  it('updates an existing invocation in expanded durable activity without raw duplicate cards or unrelated rows', async () => {
    const call = bridge({ run_activity: { events: [{ seq: 2, type: 'tool', invocation_id: 'read-one', name: 'read_file', state: 'done', result: 'Synthetic file output' }], next_cursor: 2, has_more: false, children: [] } });
    const rows = [{ ...live('root'), tools: [{ seq: 1, type: 'tool', invocation_id: 'read-one', name: 'read_file', state: 'running' }] }, live('foreign', { chat_id: 'foreign-chat', project_id: 'other-project' })];
    render(<WorkspaceActivity lives={rows} scope={scope} notify={() => {}} resume={() => {}} />);
    expect(screen.queryByText('foreign activity')).toBeNull();
    expect(call).not.toHaveBeenCalled();
    await userEvent.click(within(screen.getByRole('region', { name: 'Task activity' })).getByRole('button'));
    await screen.findByText('read file');
    expect(document.querySelectorAll('.tool-card')).toHaveLength(1);
    expect(document.querySelectorAll('.run-card')).toHaveLength(0);
    expect(call).toHaveBeenCalledWith('run_activity', { run_id: 'root', after: 0, limit: 100 });
  });
  it('keeps blocked helper recovery visible with details collapsed and gates Resume on inspection', async () => {
    let finish!: (value: unknown) => void;
    bridge({ unknown_actions: () => new Promise(resolve => { finish = resolve; }) });
    const resume = vi.fn();
    const rows = [live('root'), live('helper', { parent_id: 'root', status: 'paused', recovery: 'Inspect the synthetic output before continuing.', review: { status: 'error', summary: 'Synthetic review is unavailable.' } })];
    render(<WorkspaceActivity lives={rows} scope={scope} notify={() => {}} resume={resume} />);
    expect(screen.getByText('Helper needs attention')).toBeTruthy();
    expect(screen.getByText('Review unavailable')).toBeTruthy();
    expect(screen.getByText('Inspect the synthetic output before continuing.')).toBeTruthy();
    const button = screen.getByRole('button', { name: 'Resume' });
    expect(button).toHaveProperty('disabled', true);
    await act(async () => finish({ actions: [] }));
    expect(button).toHaveProperty('disabled', false);
    await userEvent.click(button);
    expect(resume).toHaveBeenCalledWith('helper');
  });
  it('keeps unknown actions blocked and does not promote failed earlier attempts after a successful retry', async () => {
    bridge({ unknown_actions: { actions: [{ id: 'unknown-action', name: 'write_file', arguments: {}, status: 'outcome_unknown' }] } });
    const rows = [live('root'), live('old-helper', { parent_id: 'root', status: 'failed', assignment_id: 'assignment', attempt: 1 }), live('new-helper', { parent_id: 'root', status: 'completed', assignment_id: 'assignment', attempt: 2 })];
    const rendered = render(<WorkspaceActivity lives={rows} scope={scope} notify={() => {}} resume={() => {}} />);
    expect(screen.queryByText('Helper needs attention')).toBeNull();
    rendered.rerender(<WorkspaceActivity lives={[live('blocked', { status: 'paused' })]} scope={scope} notify={() => {}} resume={() => {}} />);
    await screen.findByRole('button', { name: 'Inspect write_file' });
    expect(screen.getByRole('button', { name: 'Resume' })).toHaveProperty('disabled', true);
  });
  it('never hides an older blocked assignment behind the initial history limit', () => {
    bridge();
    const rows = Array.from({ length: 14 }, (_, index) => live(`recent-${index}`, { created_at: `2026-10-13T00:${String(59 - index).padStart(2, '0')}:00Z` }));
    rows.push(live('older-blocked', { created_at: '2026-10-12T00:00:00Z', status: 'paused' }));
    render(<WorkspaceActivity lives={rows} scope={scope} notify={() => {}} resume={() => {}} />);
    expect(screen.getByText('older-blocked activity')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Show earlier activity (2)' })).toBeTruthy();
  });
});

describe('context attachment clarity', () => {
  it('explains empty search results, clears search without losing selected IDs and restores search focus', async () => {
    bridge({ skills: { skills: [{ id: 'design', name: 'Interface design', enabled: true }] } });
    const onSkills = vi.fn(), onNotes = vi.fn();
    render(<TaskContextPanel projectId="studio" skills={['design']} notes={['note']} spaces={[{ id: 'note', name: 'Brand guide' }]} onSkills={onSkills} onNotes={onNotes} />);
    await screen.findByRole('checkbox', { name: /Interface design/ });
    expect(screen.getByText('2 attached')).toBeTruthy();
    const search = screen.getByRole('textbox', { name: 'Search skills and notes' });
    fireEvent.change(search, { target: { value: '  Missing fixture  ' } });
    expect(screen.getByText('No skills match this search.')).toBeTruthy();
    expect(screen.getByText('No notes match this search.')).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: 'Clear context search' }));
    expect(search).toHaveProperty('value', '');
    expect(document.activeElement).toBe(search);
    expect(onSkills).not.toHaveBeenCalled();
    expect(onNotes).not.toHaveBeenCalled();
    expect(screen.getByRole('checkbox', { name: /Interface design/ })).toHaveProperty('checked', true);
  });
  it('retains unavailable attachments through catalog errors and retries with the original selected identities', async () => {
    let attempts = 0;
    const call = bridge({ skills: () => { if (!attempts++) throw new Error('Synthetic catalog unavailable'); return { skills: [{ id: 'skill', library_id: 'saved-skill', name: 'Review the interface', enabled: true }] }; } });
    const onSkills = vi.fn(), onNotes = vi.fn();
    render(<TaskContextPanel projectId="studio" skills={['saved-skill']} notes={['missing-note']} spaces={[]} onSkills={onSkills} onNotes={onNotes} />);
    await screen.findByRole('alert');
    expect(screen.getByText('Unavailable skill')).toBeTruthy();
    expect(screen.getByText('Unavailable note')).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: 'Retry' }));
    await screen.findByRole('checkbox', { name: /Review the interface/ });
    expect(screen.getByRole('checkbox', { name: /Review the interface/ })).toHaveProperty('checked', true);
    expect(call).toHaveBeenNthCalledWith(2, 'skills', { project_id: 'studio' });
    expect(onSkills).not.toHaveBeenCalled();
    expect(onNotes).not.toHaveBeenCalled();
  });
});
