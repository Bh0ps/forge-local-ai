import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { ComponentProps } from 'react';
import App from '../App';
import * as loading from '../settingsLoader';
import { DEFAULT_SETTINGS } from '../types';
import { DEFAULT_LAYOUT } from '../workspaceLayout';
import { updateReadiness } from '../updateReadiness';
import { SettingsLoadError } from '../settingsAssets';

const attempt = 'a'.repeat(32);
afterEach(() => { updateReadiness.aborted(attempt); updateReadiness.dismiss(); });

function deferred<T>() { let resolve!: (value: T) => void, reject!: (error: Error) => void; const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; }
type PageProps = ComponentProps<typeof loading.SettingsPageLoader>['pageProps'];
function FixtureSettings(props: PageProps) { return <><h1>Loaded settings fixture</h1><input aria-label="Find a setting" /><output>{props.settings.model} · {props.settings.context}</output><button onClick={() => props.setTab('models')}>Change settings tab</button><output>{props.tab}</output></>; }
const loaded = { default: FixtureSettings };

async function workspace(streaming = false) {
  const chat = { id: 'lazy-settings-chat', title: 'Lazy fixture chat', project_id: null, model: 'fixture', messages: [{ role: 'assistant', content: 'Retained conversation fixture' }] };
  const projects = [{ id: 'next-project', name: 'Next fixture project', path: 'synthetic-project' }];
  const drafts = new Map<string, { revision: number; content: unknown; scope_available: boolean }>();
  drafts.set(chat.id, { revision: 1, content: { text: 'Keep this draft', document_ids: [], skills: [], space_ids: [] }, scope_available: true });
  let finishSave: ((value: unknown) => void) | undefined;
  let delaySave = false;
  const run = { id:'lazy-stream-run',chat_id:chat.id,status:'running',mode:'chat',request:'Synthetic stream fixture',rounds:1,tools:0 };
  const events: {type:string;seq:number;text:string}[] = [];
  const call = vi.fn(async (action: string, data: Record<string, unknown> = {}) => {
    if (action === 'bootstrap') return { settings: { ...DEFAULT_SETTINGS, model: 'fixture' }, projects, chats: [chat], runs: streaming ? [run] : [], capabilities: {} };
    if (action === 'poll') return {events:events.filter(event=>event.seq>Number(data.after || 0)),next_cursor:events.length,status:'running',finished:false};
    if (action === 'get_chat') return chat;
    if (action === 'setup_status') return { first_run: false };
    if (action === 'ui_layout_get') return DEFAULT_LAYOUT;
    if (action === 'ui_layout_save') return data.layout;
    if (action === 'projects') return { projects };
    if (action === 'chats') return { chats: [chat] };
    if (action === 'draft_get') return drafts.get(String(data.chat_id)) || { revision: 0, content: {}, scope_available: true };
    if (action === 'draft_save') {
      const next = { revision: Number(data.expected_revision) + 1, content: data.content, scope_available: true };
      const save = () => { drafts.set(String(data.chat_id), next); return next; };
      if (delaySave) return new Promise(resolve => { finishSave = () => resolve(save()); });
      return save();
    }
    if (action === 'pending_questions') return { questions: [] };
    if (['runs', 'schedules', 'spaces', 'models'].includes(action)) return { [action]: [] };
    return {};
  });
  window.pywebview = { api: { call } };
  render(<App />);
  await act(async () => window.dispatchEvent(new Event('pywebviewready')));
  await screen.findByText('Retained conversation fixture');
  await waitFor(() => expect((screen.getByRole('textbox', { name: 'Message Forge' }) as HTMLTextAreaElement).value).toBe('Keep this draft'));
  return { call, drafts, queueToken: (text:string)=>events.push({type:'token',seq:events.length+1,text}), delaySave: () => { delaySave = true; }, completeSave: () => finishSave?.(undefined) };
}

describe('Settings page loading in the workspace', () => {
  it('keeps the same composer and history nodes, reading position and draft behind the dialog', async () => {
    const page=deferred<typeof loaded>();vi.spyOn(loading,'loadSettingsPage').mockReturnValue(page.promise);
    const fixture=await workspace(),composer=screen.getByRole('textbox',{name:'Message Forge'}),history=screen.getByRole('region',{name:'Conversation history'});
    history.scrollTop=137;
    const loadedChats=fixture.call.mock.calls.filter(([action])=>action==='get_chat').length;
    const trigger=screen.getByRole('button',{name:'Settings'});await userEvent.click(trigger);
    const dialog=screen.getByRole('dialog',{name:'Settings'});
    expect(dialog).toHaveProperty('open',true);
    expect(document.querySelector('textarea[aria-label="Message Forge"]')).toBe(composer);
    expect(document.querySelector('.chat-scroll')).toBe(history);
    expect(history.scrollTop).toBe(137);
    expect(screen.queryByRole('textbox',{name:'Message Forge'})).toBeNull();
    await act(async()=>page.resolve(loaded));
    expect(screen.getByRole('textbox',{name:'Find a setting'})).toBe(document.activeElement);
    fireEvent(dialog,new Event('cancel',{bubbles:true,cancelable:true}));
    expect(screen.getByRole('textbox',{name:'Message Forge'})).toBe(composer);
    expect(composer).toHaveProperty('value','Keep this draft');expect(history.scrollTop).toBe(137);
    expect(document.activeElement).toBe(trigger);
    expect(fixture.call.mock.calls.filter(([action])=>action==='get_chat')).toHaveLength(loadedChats);
  });

  it('opens composer permission settings in the same dialog without replacing the conversation',async()=>{
    vi.spyOn(loading,'loadSettingsPage').mockResolvedValue(loaded);await workspace();
    const history=document.querySelector('.chat-scroll');
    await userEvent.click(screen.getByTitle('Permission profile'));
    const dialog=await screen.findByRole('dialog',{name:'Settings'});
    expect(await screen.findByText('permissions')).toBeTruthy();
    expect(document.querySelector('.chat-scroll')).toBe(history);
    fireEvent(dialog,new Event('cancel',{bubbles:true,cancelable:true}));
    expect(screen.getByRole('textbox',{name:'Message Forge'})).toHaveProperty('value','Keep this draft');
  });

  it('leaves Settings unfetched on the default chat and only requests it when opened', async () => {
    const page = deferred<typeof loaded>();
    const load = vi.spyOn(loading, 'loadSettingsPage').mockReturnValue(page.promise);
    await workspace();
    expect(load).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole('button', { name: 'Settings' }));
    expect(load).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('heading', { name: 'Loading Settings…' })).toBe(document.activeElement);
    await act(async () => page.resolve(loaded));
    await screen.findByRole('heading', { name: 'Loaded settings fixture' });
    expect(screen.getByRole('textbox', { name: 'Find a setting' })).toBe(document.activeElement);
  });

  it('finishes a hidden draft save and restores the same conversation while the page is delayed', async () => {
    const page = deferred<typeof loaded>();
    vi.spyOn(loading, 'loadSettingsPage').mockReturnValue(page.promise);
    const fixture = await workspace(); fixture.delaySave();
    fireEvent.change(screen.getByRole('textbox', { name: 'Message Forge' }), { target: { value: 'Keep the edited draft' } });
    await userEvent.click(screen.getByRole('button', { name: 'Settings' }));
    await waitFor(() => expect(fixture.call.mock.calls.some(([action]) => action === 'draft_save')).toBe(true));
    await act(async () => fixture.completeSave());
    await userEvent.click(screen.getByRole('button', { name: 'Close Settings' }));
    const composer = await screen.findByRole('textbox', { name: 'Message Forge' });
    expect((composer as HTMLTextAreaElement).value).toBe('Keep the edited draft'); expect(screen.getByRole('button', {name:'Settings'})).toBe(document.activeElement);
    await act(async () => page.resolve(loaded));
    expect(screen.queryByRole('heading', { name: 'Loaded settings fixture' })).toBeNull();
    expect(fixture.drafts.get('lazy-settings-chat')?.content).toMatchObject({ text: 'Keep the edited draft' });
  });

  it('ignores an abandoned page completion after the project changes', async () => {
    const page = deferred<typeof loaded>(); vi.spyOn(loading, 'loadSettingsPage').mockReturnValue(page.promise);
    await workspace(); await userEvent.click(screen.getByRole('button', { name: 'Settings' }));
    await userEvent.click(screen.getByRole('button', { name: 'Close Settings' }));
    await userEvent.click(screen.getByRole('button', { name: 'Next fixture project' }));
    await waitFor(() => expect(within(screen.getByRole('banner')).getByRole('button',{name:'Next fixture project'})).toBeTruthy());
    await act(async () => page.resolve(loaded));
    expect(screen.queryByRole('heading', { name: 'Loaded settings fixture' })).toBeNull();
    expect((screen.getByRole('textbox', { name: 'Message Forge' }) as HTMLTextAreaElement).value).toBe('');
    await userEvent.click(screen.getByRole('button', { name: 'Lazy fixture chat' }));
    await screen.findByText('Retained conversation fixture');
    await waitFor(() => expect((screen.getByRole('textbox', { name: 'Message Forge' }) as HTMLTextAreaElement).value).toBe('Keep this draft'));
  });

  it('keeps receiving the current run while Settings is loading', async () => {
    const page=deferred<typeof loaded>();vi.spyOn(loading,'loadSettingsPage').mockReturnValue(page.promise);
    const fixture=await workspace(true);await userEvent.click(screen.getByRole('button',{name:'Settings'}));
    fixture.queueToken('Continued while Settings loads.');
    await waitFor(()=>expect(fixture.call.mock.calls.some(([name])=>name==='poll')).toBe(true));
    await userEvent.click(screen.getByRole('button',{name:'Close Settings'}));
    expect(await screen.findByText('Continued while Settings loads.')).toBeTruthy();
    expect(fixture.call.mock.calls.some(([name])=>name==='run_cancel' || name==='run_pause')).toBe(false);
    await act(async()=>page.resolve(loaded));expect(screen.queryByRole('textbox',{name:'Find a setting'})).toBeNull();
  });

  it('retains a late native attachment in its original conversation while Settings is loading', async () => {
    const page=deferred<typeof loaded>(),capture=deferred<{image:string}>();vi.spyOn(loading,'loadSettingsPage').mockReturnValue(page.promise);
    await workspace();window.pywebview!.api.screenshot=vi.fn(()=>capture.promise);
    await userEvent.click(screen.getByRole('button',{name:'Attach screenshot'}));
    expect(window.pywebview!.api.screenshot).toHaveBeenCalledTimes(1);
    await userEvent.click(screen.getByRole('button',{name:'Settings'}));
    await act(async()=>capture.resolve({image:'c3ludGhldGlj'}));
    await userEvent.click(screen.getByRole('button',{name:'Close Settings'}));
    expect(await screen.findByRole('img',{name:'Attachment 1'})).toBeTruthy();
    expect((screen.getByRole('textbox',{name:'Message Forge'}) as HTMLTextAreaElement).value).toBe('Keep this draft');
    await act(async()=>page.resolve(loaded));expect(screen.queryByRole('textbox',{name:'Find a setting'})).toBeNull();
  });

  it('contains a chunk failure and retries it without replacing the workspace or draft', async () => {
    const page = deferred<typeof loaded>();
    const load = vi.spyOn(loading, 'loadSettingsPage').mockReturnValueOnce(page.promise).mockResolvedValue(loaded);
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});
    await workspace(); await userEvent.click(screen.getByRole('button', { name: 'Settings' }));
    await act(async () => page.reject(new Error('Synthetic failed module body: private diagnostic marker')));
    await screen.findByRole('heading', { name: 'Settings could not be opened' });
    expect(screen.queryByText(/private diagnostic marker/)).toBeNull();
    expect(screen.queryByText('The workspace needs to reload')).toBeNull();
    const retry = screen.getByRole('button', { name: 'Retry Settings' }); retry.focus();
    await userEvent.keyboard('{Enter}');
    await screen.findByRole('heading', { name: 'Loaded settings fixture' }); expect(load).toHaveBeenCalledTimes(2);
    await userEvent.keyboard('{Control>}n{/Control}');
    await screen.findByRole('textbox', { name: 'Message Forge' });
    await userEvent.click(screen.getByRole('button', { name: 'Lazy fixture chat' }));
    await screen.findByText('Retained conversation fixture');
    await waitFor(() => expect((screen.getByRole('textbox', { name: 'Message Forge' }) as HTMLTextAreaElement).value).toBe('Keep this draft'));
    consoleError.mockRestore();
  });

  it('closes Settings with local Escape without navigating away from the retained chat', async () => {
    const page = deferred<typeof loaded>(); vi.spyOn(loading, 'loadSettingsPage').mockReturnValue(page.promise);
    vi.spyOn(console, 'error').mockImplementation(() => {});
    await workspace(); await userEvent.click(screen.getByRole('button', { name: 'Settings' }));
    await act(async () => page.reject(new Error('Synthetic failed module')));
    expect(document.activeElement).toBe(await screen.findByRole('heading', { name: 'Settings could not be opened' }));
    const navigated=vi.fn(); window.addEventListener('forge:navigate',navigated);
    try { await userEvent.keyboard('{Escape}'); expect(navigated).not.toHaveBeenCalled(); }
    finally { window.removeEventListener('forge:navigate',navigated); }
    expect(screen.queryByRole('dialog',{name:'Settings'})).toBeNull();
    expect(screen.getByRole('textbox',{name:'Message Forge'})).toHaveProperty('value','Keep this draft');
    expect(document.activeElement).toBe(screen.getByRole('button',{name:'Settings'}));
  });

  it('keeps exhausted Settings recovery accessible and retains the edited draft through Return and reopening', async () => {
    vi.spyOn(loading, 'loadSettingsPage').mockRejectedValue(new SettingsLoadError('retry_exhausted'));
    vi.spyOn(console, 'error').mockImplementation(() => {});
    await workspace();
    fireEvent.change(screen.getByRole('textbox', { name: 'Message Forge' }), { target: { value: 'Keep this edited draft' } });
    await userEvent.click(screen.getByRole('button', { name: 'Settings' }));
    const heading = await screen.findByRole('heading', { name: 'Settings could not be opened' });
    expect(document.activeElement).toBe(heading);
    expect(screen.queryByRole('button', { name: 'Retry Settings' })).toBeNull();
    expect(screen.getByRole('alert').textContent).toContain('Your chat is still available. Reopen Forge');
    await act(async () => updateReadiness.start(attempt, vi.fn()));
    expect(screen.getByRole('button', { name: 'Close Settings' }).hasAttribute('disabled')).toBe(true);
    await userEvent.keyboard('{Escape}'); expect(screen.queryByRole('textbox', { name: 'Message Forge' })).toBeNull();
    await act(async () => updateReadiness.aborted(attempt));
    const returned = screen.getByRole('button', { name: 'Close Settings' }); returned.focus(); await userEvent.keyboard('{Enter}');
    const composer = await screen.findByRole('textbox', { name: 'Message Forge' });
    expect((composer as HTMLTextAreaElement).value).toBe('Keep this edited draft'); expect(document.activeElement).toBe(screen.getByRole('button', {name:'Settings'}));
    await userEvent.click(screen.getByRole('button', { name: 'Settings' }));
    await screen.findByRole('heading', { name: 'Settings could not be opened' });
    expect(screen.queryByRole('button', { name: 'Retry Settings' })).toBeNull();
    expect(screen.getByRole('alert').textContent).toBe('Your chat is still available. Reopen Forge when you’re ready to try Settings again.');
    await userEvent.keyboard('{Escape}');
    expect((await screen.findByRole('textbox', { name: 'Message Forge' }) as HTMLTextAreaElement).value).toBe('Keep this edited draft');
  });

  it('does not mount a late Settings page during a saved-work check', async () => {
    const page = deferred<typeof loaded>(); const mounted = vi.fn();
    vi.spyOn(loading, 'loadSettingsPage').mockReturnValue(page.promise);
    await workspace(); await userEvent.click(screen.getByRole('button', { name: 'Settings' }));
    await act(async () => { updateReadiness.start(attempt, vi.fn()); });
    await act(async () => page.resolve({ default: (props: PageProps) => { mounted(); return <FixtureSettings {...props} />; } }));
    expect(mounted).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'Close Settings' }).hasAttribute('disabled')).toBe(true);
    await act(async () => updateReadiness.aborted(attempt));
    await screen.findByRole('heading', { name: 'Loaded settings fixture' }); expect(mounted).toHaveBeenCalled();
  });

  it('uses current Settings properties when a delayed page finally opens', async () => {
    const page = deferred<typeof loaded>(); const load = vi.fn(() => page.promise);
    const props: PageProps = { settings: { ...DEFAULT_SETTINGS, model: 'earlier' }, onChange: vi.fn(), applyRecommendation: vi.fn(), models: [], refreshModels: vi.fn(), tab: 'general', setTab: vi.fn(), capabilities: {}, notify: vi.fn() };
    const view = render(<loading.SettingsPageLoader load={load} pageProps={props} onReturn={vi.fn()} />);
    const current = { ...props, settings: { ...props.settings, model: 'current', context: 4096 }, tab: 'models', setTab: vi.fn() };
    view.rerender(<loading.SettingsPageLoader load={load} pageProps={current} onReturn={vi.fn()} />);
    await act(async () => page.resolve(loaded));
    expect(await screen.findByText('current · 4096')).toBeTruthy(); expect(screen.getByText('models')).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: 'Change settings tab' }));
    expect(current.setTab).toHaveBeenCalledWith('models'); expect(props.setTab).not.toHaveBeenCalled(); expect(load).toHaveBeenCalledTimes(1);
  });
});
