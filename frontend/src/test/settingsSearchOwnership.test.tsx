import { useState } from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { SettingsPage } from '../settings';
import { DEFAULT_SETTINGS } from '../types';
import { findSettingsSections } from '../settingsNavigation';

const lazy = vi.hoisted(() => ({imports:0,mounts:0}));
vi.mock('../performanceSettings', () => {
  lazy.imports++;
  return {PerformanceSettings: () => { lazy.mounts++;return <h2>Deferred performance fixture</h2>; }};
});
vi.mock('../browserSettings', () => ({BrowserSettings: () => <details><summary>Optional browser setup</summary><p>Setup stays optional.</p></details>,PairBrowserSettings: () => null}));
function deferred() { let resolve!:(value:unknown)=>void;const promise=new Promise(done=>{resolve=done;});return {promise,resolve}; }
function harness(initial='shortcuts', pending?:{action:string;promise:Promise<unknown>}) {
  const onChange=vi.fn(async()=>{}),navigate=vi.fn(),refresh=vi.fn(async()=>{});
  const provider={id:'engine-fixture',name:'Local engine',kind:'openai-compatible',url:'http://127.0.0.1:1234/v1',enabled:true};
  const call=vi.fn(async(action:string,data:Record<string,unknown>={})=>{
    if(action===pending?.action)return pending.promise;
    if(action==='providers')return {providers:[provider]};
    if(action==='provider_presets')return {presets:[]};
    if(action==='show')return {capabilities:[],model_info:{}};
    throw Error('Unexpected synthetic settings action');
  });
  window.pywebview={api:{call}};
  function Workspace(){const [tab,setTab]=useState(initial);return <SettingsPage settings={{...DEFAULT_SETTINGS,model:'fixture'}} onChange={onChange} tab={tab} setTab={value=>{navigate(value);setTab(value);}} notify={()=>{}} capabilities={{}} models={[{name:'fixture'}]} refreshModels={refresh} applyRecommendation={async()=>{}}/>;}
  render(<Workspace/>);return {call,onChange,navigate,refresh};
}

describe('Settings search retains the current owner',()=>{
  beforeEach(()=>{lazy.mounts=0;});
  it('finds shipped engine aliases and precise topics from static metadata only',()=>{
    const result=findSettingsSections('Docker Model Runner');
    expect(result.map(row=>row.section.id)).toEqual(['models']);expect(result[0].topics).toContain('Engine connections');
    for(const alias of ['Jan Desktop','LocalAI','vLLM','SGLang','KoboldCpp']){const rows=findSettingsSections(alias);expect(rows.map(row=>row.section.id)).toEqual(['models']);expect(rows[0].topics).toEqual(['Engine connections']);}
    expect(findSettingsSections('temperature')[0].topics).toEqual(['Response defaults']);
    expect(findSettingsSections('rollback').map(row=>row.section.id)).toEqual(['updates']);
    expect(findSettingsSections('unmatched synthetic topic')).toEqual([]);
  });
  it('matches English API labels independently of default locale case folding',()=>{
    vi.spyOn(String.prototype,'toLocaleLowerCase').mockImplementation(function(this:string){return String(this).replace(/I/g,'\u0131').replace(/\u0130/g,'i').toLowerCase();});
    expect(findSettingsSections('API KEY').map(row=>row.section.id)).toEqual(['models','openrouter']);
    expect(findSettingsSections('OPENROUTER API KEY').map(row=>row.section.id)).toEqual(['openrouter']);
  });
  it('keeps the current section visible while counting only matching sections',async()=>{
    const {call,onChange,navigate}=harness();const search=screen.getByRole('textbox',{name:'Find a setting'});
    await userEvent.type(search,'API key');const nav=screen.getByRole('navigation',{name:'Settings sections'});
    expect(within(nav).getByRole('status').textContent).toBe('2 matching sections');
    expect(within(nav).getByRole('button',{name:'Shortcuts'}).getAttribute('aria-current')).toBe('page');
    expect(within(nav).getByText('Current')).toBeTruthy();expect(screen.getByRole('heading',{name:'Keyboard shortcuts'})).toBeTruthy();
    expect(within(nav).getByText('Engine connections')).toBeTruthy();expect(within(nav).getByText('Cloud helpers & consent')).toBeTruthy();
    expect(within(screen.getByRole('combobox',{name:'Settings section'})).getByRole('option',{name:'Shortcuts · Current'})).toBeTruthy();
    expect(navigate).not.toHaveBeenCalled();expect(onChange).not.toHaveBeenCalled();expect(call).not.toHaveBeenCalled();
  });
  it('preserves download text and the same model-page nodes through matches, no matches and clearing',async()=>{
    const {call,onChange,navigate}=harness('models');await screen.findByRole('button',{name:'Configure'});
    const download=screen.getByRole('textbox',{name:'Model to download'}),engine=screen.getByRole('combobox',{name:'Active engine'});
    await userEvent.type(download,'synthetic-model');const initial=call.mock.calls.length;
    const search=screen.getByRole('textbox',{name:'Find a setting'});
    for(const query of ['hardware','unmatched synthetic topic','']){await userEvent.clear(search);if(query)await userEvent.type(search,query);expect(screen.getByRole('textbox',{name:'Model to download'})).toBe(download);expect(download).toHaveProperty('value','synthetic-model');expect(screen.getByRole('combobox',{name:'Active engine'})).toBe(engine);}
    expect(call.mock.calls.length).toBe(initial);expect(onChange).not.toHaveBeenCalled();expect(navigate).not.toHaveBeenCalled();
  });
  it('does not load or mount a matching deferred section until explicit keyboard navigation',async()=>{
    const {call,navigate}=harness();const imports=lazy.imports;await userEvent.type(screen.getByRole('textbox',{name:'Find a setting'}),'hardware');
    expect(lazy.imports).toBe(imports);expect(lazy.mounts).toBe(0);expect(call).not.toHaveBeenCalled();expect(navigate).not.toHaveBeenCalled();
    const button=within(screen.getByRole('navigation',{name:'Settings sections'})).getByRole('button',{name:'Performance'});button.focus();await userEvent.keyboard('{Enter}');
    await screen.findByRole('heading',{name:'Deferred performance fixture'});expect(navigate).toHaveBeenCalledOnce();expect(navigate).toHaveBeenCalledWith('performance');expect(lazy.mounts).toBeGreaterThan(0);
  });
  for(const action of ['provider_test','provider_save'])it(`does not replace a pending ${action} editor or replay its action on background search changes`,async()=>{
    const pending=deferred(),{call}=harness('models',{action,promise:pending.promise});
    await userEvent.click(await screen.findByRole('button',{name:'Configure'}));const dialog=screen.getByRole('dialog'),name=screen.getByRole('textbox',{name:'Name'}),key=screen.getByLabelText('API key (optional)');
    await userEvent.type(key,'synthetic input');await userEvent.click(screen.getByRole('button',{name:action==='provider_test'?'Test connection':'Save engine'}));
    await waitFor(()=>expect(call.mock.calls.filter(([name])=>name===action)).toHaveLength(1));const count=call.mock.calls.length;
    // A dialog owns normal keyboard focus. This deliberately simulates a background
    // query rerender while its request is pending, rather than claiming focus leaves it.
    const search=screen.getByRole('textbox',{name:'Find a setting'});fireEvent.change(search,{target:{value:'temperature'}});fireEvent.change(search,{target:{value:'unmatched synthetic topic'}});fireEvent.change(search,{target:{value:''}});
    expect(screen.getByRole('dialog')).toBe(dialog);expect(screen.getByRole('textbox',{name:'Name'})).toBe(name);expect(screen.getByLabelText('API key (optional)')).toBe(key);
    expect(key).toHaveProperty('value','synthetic input');expect(key).toHaveProperty('disabled',true);expect(call.mock.calls.length).toBe(count);
    const request=call.mock.calls.find(([name])=>name===action)![1]!;await act(async()=>pending.resolve(action==='provider_test'?{ok:true,model_count:1}:{id:request.id}));
    expect(call.mock.calls.filter(([name])=>name===action)).toHaveLength(1);
    if(action==='provider_save')await waitFor(()=>expect(screen.queryByRole('dialog')).toBeNull());else expect(key).toHaveProperty('disabled',false);
  });
  it('clears search with Escape or the labeled clear action without changing settings',async()=>{
    const {call,onChange,navigate}=harness();const search=screen.getByRole('textbox',{name:'Find a setting'});
    await userEvent.type(search,'memory');await userEvent.keyboard('{Escape}');expect(search).toHaveProperty('value','');expect(document.activeElement).toBe(search);
    await userEvent.type(search,'memory');await userEvent.click(screen.getByRole('button',{name:'Clear settings search'}));expect(search).toHaveProperty('value','');expect(document.activeElement).toBe(search);
    expect(call).not.toHaveBeenCalled();expect(onChange).not.toHaveBeenCalled();expect(navigate).not.toHaveBeenCalled();
  });
  it('does not open optional setup disclosures when their topics match',async()=>{
    const {call,navigate}=harness('browser');const disclosure=screen.getByText('Optional browser setup').closest('details')!;
    await userEvent.type(screen.getByRole('textbox',{name:'Find a setting'}),'Chrome');expect(disclosure.open).toBe(false);expect(screen.getByText('Optional browser setup').closest('details')).toBe(disclosure);
    expect(call).not.toHaveBeenCalled();expect(navigate).not.toHaveBeenCalled();
  });
});
