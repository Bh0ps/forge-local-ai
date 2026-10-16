import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { PluginsPage, type LibraryEntry } from '../pluginsPage';

afterEach(()=>{cleanup();vi.restoreAllMocks();});
const entry:LibraryEntry={id:'starter:review',name:'Review changes',description:'Inspect the current changes and preserve local work.',kind:'skill',category:'review',bundle:'review',source:'builtin:skill/review',catalog_name:'Forge starters',license:'MIT',builtin:true,installed:true,enabled:true,automatic:false,skill_id:'review'};
function deferred<T>() {let resolve!:(value:T)=>void;const promise=new Promise<T>(done=>{resolve=done;});return {promise,resolve};}
function bridge(overrides:Record<string,unknown|((data:Record<string,unknown>)=>unknown)>={}) {
 const responses:Record<string,unknown>={catalog_discover:{entries:[],sources:[]},skills:{skills:[]},plugins:{plugins:[]},...overrides};
 const call=vi.fn(async(action:string,data:Record<string,unknown>={})=>{if(!(action in responses))throw new Error(`Unexpected synthetic action: ${action}`);const value=responses[action];return typeof value==='function'?value(data):value;});
 window.pywebview={api:{call}};return {call,responses};
}

describe('Library current loading and error copy',()=>{
 it('does not claim an empty Installed collection while its reads are still pending',async()=>{
  const skills=deferred<unknown>(),plugins=deferred<unknown>();bridge({skills:()=>skills.promise,plugins:()=>plugins.promise});
  render(<PluginsPage notify={vi.fn()}/>);await userEvent.click(screen.getByRole('button',{name:/Installed/}));
  expect(screen.queryByRole('heading',{name:'Start your collection'})).toBeNull();
  expect(screen.getByRole('status').textContent).toContain('Loading your collection');
  expect(screen.queryByText('0 items')).toBeNull();expect(screen.queryByText(/^0 enabled/)).toBeNull();
  await act(async()=>{skills.resolve({skills:[]});plugins.resolve({plugins:[]});});
  expect(await screen.findByRole('heading',{name:'Start your collection'})).toBeTruthy();
 });
 it('shows the initial discovery failure without simultaneously claiming no library items exist',async()=>{
  const {call}=bridge({catalog_discover:()=>{throw new Error('Synthetic catalog offline');}});
  render(<PluginsPage notify={vi.fn()}/>);expect(await screen.findByRole('alert')).toBeTruthy();
  expect(screen.queryByRole('heading',{name:'No library items available'})).toBeNull();
  expect(screen.getAllByRole('button',{name:'Retry'})).toHaveLength(1);
  expect(call.mock.calls.filter(([action])=>action==='catalog_discover')).toHaveLength(1);
 });
 it('shows an Installed read failure without presenting an empty-collection invitation',async()=>{
  bridge({skills:()=>{throw new Error('Synthetic installed read failed');}});
  render(<PluginsPage notify={vi.fn()}/>);await userEvent.click(screen.getByRole('button',{name:/Installed/}));
  expect(await screen.findByRole('alert')).toBeTruthy();expect(screen.queryByRole('heading',{name:'Start your collection'})).toBeNull();
 });
 it('keeps the true empty collection invitation after successful reads',async()=>{
  bridge();render(<PluginsPage notify={vi.fn()}/>);await userEvent.click(screen.getByRole('button',{name:/Installed/}));
  expect(await screen.findByRole('heading',{name:'Start your collection'})).toBeTruthy();expect(screen.queryByRole('alert')).toBeNull();
 });
 it('distinguishes a successful search with no matches and clears only local filters',async()=>{
  const {call}=bridge({catalog_discover:{entries:[entry],sources:[]}});render(<PluginsPage notify={vi.fn()}/>);
  await screen.findByRole('heading',{name:entry.name});const before=call.mock.calls.length;
  await userEvent.type(screen.getByRole('textbox',{name:'Search library'}),'unmatched');
  expect(screen.getByRole('heading',{name:'No matching items'})).toBeTruthy();await userEvent.click(screen.getByRole('button',{name:'Clear filters'}));
  expect(screen.getByRole('heading',{name:entry.name})).toBeTruthy();expect(call.mock.calls).toHaveLength(before);
 });
});

describe('Library presentation preserves explicit workflows',()=>{
 it('shares Coding aliases and exposes custom bundles without catalog reads on filter changes',async()=>{
  const cpp={...entry,id:'cpp',name:'Native C++',bundle:'build',category:'coding',description:'Use CMake presets.',skill_id:undefined};
  const dotnet={...cpp,id:'dotnet',name:'Windows .NET',bundle:'development',description:'Use the solution build.'};
  const custom={...cpp,id:'custom',name:'Training report',bundle:'machine_learning',description:'Compare training runs.'};
  const {call}=bridge({catalog_discover:{entries:[cpp,dotnet,custom],sources:[]}});render(<PluginsPage notify={vi.fn()}/>);
  await screen.findByRole('heading',{name:'Native C++'});const before=call.mock.calls.length;
  expect(screen.getAllByRole('button',{name:'Coding bundle'})).toHaveLength(1);
  expect(screen.getByRole('option',{name:'Machine Learning'})).toBeTruthy();
  await userEvent.selectOptions(screen.getByRole('combobox',{name:'Workflow bundle'}),'coding');
  expect(screen.getByRole('heading',{name:'Windows .NET'})).toBeTruthy();expect(screen.queryByRole('heading',{name:'Training report'})).toBeNull();
  const group=screen.getByRole('button',{name:'Coding bundle'});await userEvent.click(group);
  expect(screen.queryByRole('heading',{name:'Native C++'})).toBeNull();
  await userEvent.type(screen.getByRole('textbox',{name:'Search library'}),'cmake');
  expect(screen.getByRole('heading',{name:'Native C++'})).toBeTruthy();expect(call.mock.calls).toHaveLength(before);
  await userEvent.clear(screen.getByRole('textbox',{name:'Search library'}));
  await userEvent.selectOptions(screen.getByRole('combobox',{name:'Workflow bundle'}),'machine-learning');
  expect(screen.getByRole('heading',{name:'Training report'})).toBeTruthy();expect(call.mock.calls).toHaveLength(before);
 });
 it('names real view/filter groups and filters locally through their original controls',async()=>{
  const {call}=bridge({catalog_discover:{entries:[entry],sources:[]}});render(<PluginsPage notify={vi.fn()}/>);
  await screen.findByRole('heading',{name:entry.name});const before=call.mock.calls.length;
  expect(screen.getByRole('group',{name:'Library views'})).toBeTruthy();expect(screen.getByRole('group',{name:'Library filters'})).toBeTruthy();
  const types=screen.getByRole('group',{name:'Extension type'});
  const plugins=within(types).getByRole('button',{name:'Plugins'});plugins.focus();await userEvent.keyboard('{Enter}');
  expect(plugins.getAttribute('aria-pressed')).toBe('true');expect(screen.getByRole('heading',{name:'No matching items'})).toBeTruthy();
  await userEvent.click(within(types).getByRole('button',{name:'Skills'}));await userEvent.selectOptions(screen.getByRole('combobox',{name:'Workflow bundle'}),'review');
  await userEvent.selectOptions(screen.getByRole('combobox',{name:'Skill category'}),'review');
  expect(screen.getByRole('heading',{name:entry.name})).toBeTruthy();expect(call.mock.calls).toHaveLength(before);
 });
 it('describes each preview with its item name and reads instructions only after an explicit request',async()=>{
  const {call}=bridge({catalog_discover:{entries:[entry],sources:[]},skill_preview:{text:'# Review\nKeep the current work and verify the changed files.'}});
  render(<PluginsPage notify={vi.fn()}/>);const card=await screen.findByRole('article',{name:entry.name});
  const preview=within(card).getByRole('button',{name:entry.name}),description=preview.getAttribute('aria-describedby');
  expect(description).toBeTruthy();expect(document.getElementById(description!)?.textContent).toBe(entry.description);
  expect(within(card).getAllByRole('button')).toHaveLength(1);expect(within(card).queryByRole('switch')).toBeNull();
  expect(within(card).queryByText('MIT')).toBeNull();
  expect(call.mock.calls.some(([action])=>action==='skill_preview')).toBe(false);preview.focus();await userEvent.keyboard('{Enter}');
  const dialog=await screen.findByRole('dialog',{name:entry.name});expect(await within(dialog).findByText('Keep the current work and verify the changed files.')).toBeTruthy();expect(within(dialog).getByText('MIT')).toBeTruthy();
  expect(call.mock.calls.filter(([action])=>action==='skill_preview')).toEqual([['skill_preview',{id:'review',project_id:undefined}]]);
  expect(call.mock.calls.some(([action])=>['skill_toggle','plugin_install','skill_edit','skill_install'].includes(action))).toBe(false);
  // jsdom does not synthesize the browser's native dialog cancel event from Escape.
  fireEvent(dialog,new Event('cancel',{bubbles:true}));await waitFor(()=>expect(screen.queryByRole('dialog')).toBeNull());expect(document.activeElement).toBe(preview);
 });
 it('reveals a keyboard/hover Refresh label without reads, then preserves explicit refresh and disabled state',async()=>{
  const refreshRead=deferred<unknown>();const {call}=bridge({catalog_discover:(data:Record<string,unknown>)=>data.refresh?refreshRead.promise:{entries:[entry],sources:[]}});
  render(<PluginsPage notify={vi.fn()}/>);await screen.findByRole('heading',{name:entry.name});const refresh=screen.getByRole('button',{name:'Refresh library'});
  await waitFor(()=>expect((refresh as HTMLButtonElement).disabled).toBe(false));const before=call.mock.calls.length;
  act(()=>refresh.focus());expect(screen.getByText('Refresh library').getAttribute('aria-hidden')).toBe('true');await userEvent.keyboard('{Escape}');
  expect(screen.queryByText('Refresh library')).toBeNull();expect(document.activeElement).toBe(refresh);expect(call.mock.calls).toHaveLength(before);
  act(()=>refresh.blur());await userEvent.hover(refresh);expect(screen.getByText('Refresh library').getAttribute('aria-hidden')).toBe('true');await userEvent.unhover(refresh);
  expect(call.mock.calls).toHaveLength(before);await userEvent.click(refresh);await waitFor(()=>expect((refresh as HTMLButtonElement).disabled).toBe(true));
  expect(screen.queryByText('Refresh library')).toBeNull();expect(screen.getByRole('heading',{name:entry.name})).toBeTruthy();
  expect(call.mock.calls.filter(([action])=>action==='catalog_discover')).toHaveLength(2);
  await act(async()=>refreshRead.resolve({entries:[entry],sources:[]}));await waitFor(()=>expect((refresh as HTMLButtonElement).disabled).toBe(false));
 });
 it('keeps cached entries visible after refresh failure without auto retry or an empty-state claim',async()=>{
  const {call}=bridge({catalog_discover:(data:Record<string,unknown>)=>{if(data.refresh)throw new Error('Synthetic refresh offline');return {entries:[entry],sources:[]};}});
  render(<PluginsPage notify={vi.fn()}/>);await screen.findByRole('heading',{name:entry.name});await userEvent.click(screen.getByRole('button',{name:'Refresh library'}));
  expect(await screen.findByRole('alert')).toBeTruthy();expect(screen.getByRole('article',{name:entry.name})).toBeTruthy();expect(screen.queryByRole('heading',{name:'No library items available'})).toBeNull();
  expect(call.mock.calls.filter(([action])=>action==='catalog_discover')).toHaveLength(2);
 });
 it('keeps a pending readonly preview in the same modal through a harmless parent update',async()=>{
  const read=deferred<unknown>();const {call}=bridge({catalog_discover:{entries:[entry],sources:[]},skill_preview:()=>read.promise});
  const view=render(<PluginsPage notify={vi.fn()} projectId="studio"/>);await screen.findByRole('heading',{name:entry.name});
  await userEvent.click(screen.getByRole('button',{name:entry.name}));const dialog=screen.getByRole('dialog',{name:entry.name});expect(within(dialog).getByRole('status').textContent).toContain('Reading skill');
  view.rerender(<PluginsPage notify={vi.fn()} projectId="studio"/>);expect(screen.getByRole('dialog',{name:entry.name})).toBe(dialog);
  await act(async()=>read.resolve({text:'# Review\nKeep the original instruction evidence.'}));expect(await within(dialog).findByText('Keep the original instruction evidence.')).toBeTruthy();
  expect(call.mock.calls.filter(([action])=>action==='skill_preview')).toEqual([['skill_preview',{id:'review',project_id:'studio'}]]);
 });
 it('retains the source and staged review while inspection is pending and discards exactly that reviewed token on Cancel',async()=>{
  const inspect=deferred<unknown>();const {call}=bridge({plugin_inspect:()=>inspect.promise,plugin_discard:{ok:true}});
  const view=render(<PluginsPage notify={vi.fn()}/>);await userEvent.click(screen.getByRole('button',{name:'Import plugin'}));
  const source=screen.getByRole('textbox',{name:'Source'});await userEvent.type(source,'https://synthetic.invalid/plugin');await userEvent.click(screen.getByRole('button',{name:'Inspect source'}));
  view.rerender(<PluginsPage notify={vi.fn()}/>);expect(screen.getByRole('textbox',{name:'Source'})).toBe(source);expect((source as HTMLInputElement).value).toBe('https://synthetic.invalid/plugin');expect((source as HTMLInputElement).disabled).toBe(true);
  expect(call.mock.calls.some(([action])=>action==='plugin_install')).toBe(false);
  await act(async()=>inspect.resolve({inspection_id:'review-token',name:'Synthetic plugin',compatible:true,compatibility:'Portable skills',warnings:['Review the connection setup.']}));
  expect(await screen.findByText('Portable skills')).toBeTruthy();await userEvent.click(screen.getByRole('button',{name:'Cancel'}));
  await waitFor(()=>expect(call).toHaveBeenCalledWith('plugin_discard',{inspection_id:'review-token'}));expect(call.mock.calls.filter(([action])=>action==='plugin_discard')).toHaveLength(1);
  expect(call.mock.calls.some(([action])=>action==='plugin_install')).toBe(false);
 });
});
