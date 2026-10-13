import { useState } from 'react';
import { act,fireEvent,render,screen,waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe,expect,it,vi } from 'vitest';
import { ContextControl } from '../contextControl';
import { ContextSettings } from '../contextSettings';
import { contextBand,reasoningLabel } from '../contextPresentation';
import { DEFAULT_SETTINGS,type Settings } from '../types';
import { updateReadiness } from '../updateReadiness';
// Test-only Node API; production has no Node type/runtime dependency.
const {readFileSync}=await import(/* @vite-ignore */ 'node:'+'fs') as {readFileSync(path:string,encoding:'utf8'):string};
const contextCss=readFileSync('src/contextSettings.css','utf8');
const tokensCss=readFileSync('src/tokens.css','utf8');

function deferred<T=void>(){let resolve!:(value:T)=>void,reject!:(error:Error)=>void;const promise=new Promise<T>((yes,no)=>{resolve=yes;reject=no;});return{promise,resolve,reject};}
function bridge(show:unknown={}){const call=vi.fn(async(action:string)=>action==='settings'?DEFAULT_SETTINGS:show);window.pywebview={api:{call}};return call;}
const settings={...DEFAULT_SETTINGS,model:'synthetic-model',provider_id:'local'};
describe('context controls',()=>{
  it('keeps every configured-capacity stroke readable on real light and dark surfaces without motion',()=>{
    const luminance=(hex:string)=>{const full=hex.length===3?hex.split('').map(char=>char+char).join(''):hex;return full.match(/../g)!.map(value=>parseInt(value,16)/255).map(value=>value<=.04045?value/12.92:((value+.055)/1.055)**2.4).reduce((sum,value,index)=>sum+value*[.2126,.7152,.0722][index],0);};
    const inks=[...contextCss.matchAll(/--context-ink:#([a-f0-9]+)\}/g)].map(match=>match[1]);const surfaces=[...tokensCss.matchAll(/--surface:#([a-f0-9]+);/g)].map(match=>match[1]);
    expect(inks).toHaveLength(10);expect(surfaces).toHaveLength(2);
    inks.forEach((ink,index)=>{const a=luminance(ink),b=luminance(surfaces[index<5?0:1]);expect((Math.max(a,b)+.05)/(Math.min(a,b)+.05)).toBeGreaterThanOrEqual(4.5);});
    expect(contextCss).not.toMatch(/animation|transition|@keyframes/);
  });
  it('uses configured capacity bands independently of requested reasoning',()=>{
    expect([2048,4096,8192,16384,32768,65536,131072,262144].map(value=>contextBand(value).accent)).toEqual(['sage','blue','blue','copper','copper','violet','violet','rose']);
    const unknown={limit:null,values:[],mandatory:false,known:false};
    expect(reasoningLabel(false,unknown)).toContain('Off · support unreported');
    expect(reasoningLabel(false,{...unknown,values:[true]})).toBe('Reasoning: Required');
    expect(reasoningLabel(true,{...unknown,values:[false]})).toBe('Reasoning: Unavailable');
    expect(reasoningLabel(true,{...unknown,controlSupported:false})).toBe('Reasoning: Engine controlled');
  });
  it('preserves the saved preference when reasoning is explicitly engine controlled, and separates maximum from loaded context',async()=>{
    bridge({context_limit:8192,context_limit_source:'loaded_instance',reasoning:{control_supported:false,mandatory:false,advertised_options:['on','off']},advertised_metadata:{max_context_length:131072,loaded_instances:[{model:'loaded-instance',context_length:8192}]}});
    const change=vi.fn(async()=>{});render(<ContextSettings settings={settings} onChange={change} onReasoningChange={change}/>);
    await waitFor(()=>expect((screen.getByRole('switch',{name:'Model reasoning'}) as HTMLButtonElement).disabled).toBe(true));expect(screen.getByRole('switch',{name:'Model reasoning'}).getAttribute('aria-checked')).toBe('true');expect(screen.getByText(/Advertised maximum: 128K/)).toBeTruthy();expect(screen.getByText(/loaded-instance · 8K loaded/)).toBeTruthy();expect(change).not.toHaveBeenCalled();
  });
  it('opens at the current preset, links the dialog and restores Escape focus',async()=>{
    bridge();function Harness(){const [open,setOpen]=useState(false);return <><button>Other control</button><ContextControl settings={settings} onChange={async()=>{}} open={open} setOpen={setOpen}/></>;}
    render(<Harness/>);const trigger=screen.getByRole('button',{name:/Context controls/});
    expect(trigger.getAttribute('data-context-accent')).toBe('copper');await userEvent.click(trigger);
    expect(trigger.getAttribute('aria-controls')).toBe(screen.getByRole('dialog').id);
    expect(document.activeElement).toBe(screen.getByRole('button',{name:'High 32K'}));
    fireEvent.keyDown(window,{key:'Escape'});expect(document.activeElement).toBe(trigger);expect(trigger.getAttribute('aria-expanded')).toBe('false');
    await userEvent.click(trigger);const other=screen.getByRole('button',{name:'Other control'});fireEvent.pointerDown(other);other.focus();expect(screen.queryByRole('dialog')).toBeNull();expect(document.activeElement).toBe(other);
    await userEvent.click(trigger);other.focus();fireEvent.keyDown(window,{key:'Escape'});expect(screen.queryByRole('dialog')).toBeNull();expect(document.activeElement).toBe(other);
  });
  it('coalesces slider edits and serializes a newer value behind the admitted save',async()=>{
    bridge();const first=deferred(),save=vi.fn().mockReturnValueOnce(first.promise).mockResolvedValue(undefined);
    render(<ContextSettings settings={settings} onChange={save}/>);const slider=screen.getByRole('slider');
    fireEvent.change(slider,{target:{value:49152}});fireEvent.change(slider,{target:{value:65536}});expect(save).not.toHaveBeenCalled();fireEvent.pointerUp(slider);
    expect(save).toHaveBeenCalledExactlyOnceWith({context:65536});fireEvent.change(slider,{target:{value:131072}});fireEvent.pointerUp(slider);expect(save).toHaveBeenCalledTimes(1);
    await act(async()=>first.resolve());await waitFor(()=>expect(save).toHaveBeenCalledTimes(2));expect(save).toHaveBeenLastCalledWith({context:131072});
  });
  it('keeps slider focus through parent callbacks and settings rerenders',async()=>{
    bridge();const view=render(<ContextControl settings={settings} onChange={async()=>{}} open={true} setOpen={()=>{}}/>),slider=screen.getByRole('slider');slider.focus();
    view.rerender(<ContextControl settings={{...settings,thinking:false}} onChange={async()=>{}} open={true} setOpen={()=>{}}/>);expect(document.activeElement).toBe(slider);
  });
  it('retains a failed optimistic value after closing, and only an actual retry ACK clears readiness',async()=>{
    bridge();const retry=deferred();const save=vi.fn().mockRejectedValueOnce(new Error('synthetic refusal')).mockReturnValueOnce(retry.promise);
    function Harness(){const [open,setOpen]=useState(true),[value,setValue]=useState(settings);return <ContextControl settings={value} open={open} setOpen={setOpen} onChange={delta=>{setValue(current=>({...current,...delta}));return save(delta);}}/>;}
    render(<Harness/>);await userEvent.click(screen.getByRole('button',{name:'Max 128K'}));await screen.findByRole('alert');fireEvent.keyDown(window,{key:'Escape'});
    expect(updateReadiness.diagnose().participants.some(row=>row.reasons.includes('preferences_pending'))).toBe(true);
    await userEvent.click(screen.getByRole('button',{name:/Context controls/}));expect((screen.getByRole('slider') as HTMLInputElement).value).toBe('131072');await userEvent.click(screen.getByRole('button',{name:'Retry context save'}));
    expect(save).toHaveBeenCalledTimes(2);expect(updateReadiness.diagnose().participants.some(row=>row.state==='waiting')).toBe(true);
    await act(async()=>retry.resolve());await waitFor(()=>expect(screen.queryByRole('alert')).toBeNull());expect(updateReadiness.diagnose().participants.some(row=>row.reasons.includes('preferences_pending'))).toBe(false);
  });
  it('restores the last acknowledged value with a real save when a change is discarded',async()=>{
    bridge();const save=vi.fn().mockRejectedValueOnce(new Error('refused')).mockResolvedValue(undefined);render(<ContextSettings settings={settings} onChange={save}/>);
    await userEvent.click(screen.getByRole('button',{name:'Low 2K'}));await screen.findByRole('alert');await userEvent.click(screen.getByRole('button',{name:'Discard context change'}));
    await waitFor(()=>expect(save).toHaveBeenLastCalledWith({context:32768}));expect((screen.getByRole('slider') as HTMLInputElement).value).toBe('32768');
  });
  it('does not let a delayed discard read replace a newer context selection',async()=>{
    const saved=deferred<Settings>();window.pywebview={api:{call:vi.fn(action=>action==='settings'?saved.promise:Promise.resolve({}))}};const save=vi.fn().mockRejectedValueOnce(new Error('refused')).mockResolvedValue(undefined);render(<ContextSettings settings={settings} onChange={save}/>);
    await userEvent.click(screen.getByRole('button',{name:'Low 2K'}));await screen.findByRole('alert');await userEvent.click(screen.getByRole('button',{name:'Discard context change'}));await userEvent.click(screen.getByRole('button',{name:'Max 128K'}));await act(async()=>saved.resolve(DEFAULT_SETTINGS));
    await waitFor(()=>expect(save).toHaveBeenLastCalledWith({context:131072}));expect(save).toHaveBeenCalledTimes(2);expect((screen.getByRole('slider') as HTMLInputElement).value).toBe('131072');
  });
  it('honors a newer A→B→A selection while an earlier discard read is pending',async()=>{
    const saved=deferred<Settings>();window.pywebview={api:{call:vi.fn(action=>action==='settings'?saved.promise:Promise.resolve({}))}};const save=vi.fn().mockRejectedValueOnce(new Error('refused')).mockResolvedValue(undefined);render(<ContextSettings settings={settings} onChange={save}/>);
    await userEvent.click(screen.getByRole('button',{name:'Low 2K'}));await screen.findByRole('alert');await userEvent.click(screen.getByRole('button',{name:'Discard context change'}));await userEvent.click(screen.getByRole('button',{name:'Max 128K'}));await userEvent.click(screen.getByRole('button',{name:'Low 2K'}));await act(async()=>saved.resolve(DEFAULT_SETTINGS));await waitFor(()=>expect(save).toHaveBeenLastCalledWith({context:2048}));expect(save).toHaveBeenCalledTimes(2);expect((screen.getByRole('slider') as HTMLInputElement).value).toBe('2048');
  });
  it('preserves the latest global context while rejecting a prior model metadata response',async()=>{
    const oldShow=deferred<Record<string,unknown>>(),oldSave=deferred();window.pywebview={api:{call:vi.fn((_,data)=>data?.model==='synthetic-model'?oldShow.promise:Promise.resolve({context_length:8192}))}};
    const save=vi.fn().mockReturnValueOnce(oldSave.promise).mockResolvedValue(undefined),view=render(<ContextSettings settings={settings} onChange={save}/>);
    await userEvent.click(screen.getByRole('button',{name:'Max 128K'}));view.rerender(<ContextSettings settings={{...settings,model:'new-model',context:8192}} onChange={save}/>);await userEvent.click(screen.getByRole('button',{name:'Medium 8K'}));
    await act(async()=>{oldShow.resolve({context_length:262144});oldSave.resolve();});
    expect((screen.getByRole('slider') as HTMLInputElement).value).toBe('8192');expect(screen.getByText(/reports up to 8K/)).toBeTruthy();
  });
  it('keeps a failed global context visible across models and retires it only with an exact context ACK',async()=>{
    bridge();const save=vi.fn().mockRejectedValueOnce(new Error('refused')).mockResolvedValue(undefined);const view=render(<ContextSettings settings={settings} onChange={save}/>);await userEvent.click(screen.getByRole('button',{name:'Max 128K'}));await screen.findByRole('alert');
    view.rerender(<ContextSettings settings={{...settings,model:'new-model',thinking:false,context:131072}} onChange={save}/>);expect(screen.getByRole('alert')).toBeTruthy();expect(updateReadiness.diagnose().participants.some(row=>row.reasons.includes('preferences_pending'))).toBe(true);
    await userEvent.click(screen.getByRole('button',{name:'Medium 8K'}));await waitFor(()=>expect(screen.queryByRole('alert')).toBeNull());expect(save).toHaveBeenLastCalledWith({context:8192});expect(updateReadiness.diagnose().participants.some(row=>row.reasons.includes('preferences_pending'))).toBe(false);
  });
  it('retries failed model metadata and retains exact custom capacity in the accessible label',async()=>{
    const call=vi.fn().mockRejectedValueOnce(new Error('unavailable')).mockResolvedValue({thinking:{values:[true]},context_length:65536});window.pywebview={api:{call}};
    render(<ContextControl settings={{...settings,context:12288,thinking:false}} onChange={async()=>{}} open={true} setOpen={()=>{}}/>);
    expect(screen.getByRole('button',{name:/Context controls/}).getAttribute('aria-label')).toContain('12,288 tokens configured');await userEvent.click(await screen.findByRole('button',{name:'Retry model details'}));
    await waitFor(()=>expect(screen.getByRole('button',{name:/Context controls/}).getAttribute('aria-label')).toContain('Reasoning: Required'));
  });
  it.each(['success','failure'])('requires explicit native reconciliation after a late browser %s and retires its metadata',async outcome=>{
    delete window.pywebview;const oldSave=deferred(),oldShow=deferred<Record<string,unknown>>(),currentSave=deferred();vi.spyOn(globalThis,'fetch').mockResolvedValue({ok:true,json:()=>oldShow.promise} as Response);
    const browserSave=vi.fn(()=>oldSave.promise),nativeSave=vi.fn(()=>currentSave.promise);const view=render(<ContextSettings settings={settings} onChange={browserSave}/>);await userEvent.click(screen.getByRole('button',{name:'Max 128K'}));
    bridge({context_length:8192});view.rerender(<ContextSettings settings={settings} onChange={nativeSave}/>);expect((screen.getByRole('button',{name:'Retry context save'}) as HTMLButtonElement).disabled).toBe(true);expect(nativeSave).not.toHaveBeenCalled();
    await act(async()=>{oldShow.resolve({context_length:262144});if(outcome==='success')oldSave.resolve();else oldSave.reject(new Error('private old response'));});
    expect(screen.getByText(/Confirm your unsaved context selection/)).toBeTruthy();expect(screen.getByText(/reports up to 8K/)).toBeTruthy();expect(updateReadiness.diagnose().participants.some(row=>row.reasons.includes('preferences_pending'))).toBe(true);expect(nativeSave).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole('button',{name:'Retry context save'}));expect(nativeSave).toHaveBeenCalledExactlyOnceWith({context:131072});await act(async()=>currentSave.resolve());expect(screen.queryByRole('alert')).toBeNull();expect(updateReadiness.diagnose().participants.some(row=>row.reasons.includes('preferences_pending'))).toBe(false);
  });
});
