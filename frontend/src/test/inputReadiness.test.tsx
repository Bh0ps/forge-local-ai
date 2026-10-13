import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen } from '@testing-library/react';
import { api, native } from '../api';
import { updateReadiness } from '../updateReadiness';
import { useRetainedScratch } from '../retainedScratch';
import { AppearanceSettings } from '../appearance';
import { DEFAULT_SETTINGS } from '../types';
const attempt='a'.repeat(32);
afterEach(()=>{updateReadiness.aborted(attempt);delete window.pywebview;});
describe('input and asynchronous ownership during restart',()=>{
  it('tracks a stored diagnostics export as a mutation until its artifact reply settles',async()=>{
    let complete!:(value:unknown)=>void;window.pywebview={api:{call:vi.fn(()=>new Promise(resolve=>{complete=resolve;}))}};
    const exporting=api('diagnostics_export');updateReadiness.start(attempt,vi.fn());expect(await updateReadiness.confirm(attempt)).toEqual({ok:false,reason:'submission_pending'});complete({diagnostics:{version:'fixture'},artifact_id:'fixture'});await exporting;expect(updateReadiness.canEdit()).toBe(false);
  });
  it('blocks user mutations and native input while keeping read-only polls responsive',async()=>{
    const call=vi.fn(async()=>({})),mode=vi.fn(async()=>({}));window.pywebview={api:{call,mode}};updateReadiness.start(attempt,vi.fn());expect((await updateReadiness.confirm(attempt)).ok).toBe(true);
    await expect(api('start_chat',{text:'Synthetic private text'})).rejects.toThrow('confirming a restart');await expect(native('mode','hud')).rejects.toThrow('confirming a restart');
    await api('poll',{id:'fixture'});expect(call.mock.calls).toEqual([['poll',{id:'fixture'}]]);expect(mode).not.toHaveBeenCalled();
  });
  it('retains scratch across view changes and never accepts newer edits as an older completed export',async()=>{
    let scratch:ReturnType<typeof useRetainedScratch<{body:string}>>;function Harness({id}:{id:string}){scratch=useRetainedScratch(id,{body:''});return <input aria-label="Export fixture" value={scratch.value.body} onChange={event=>scratch.update({body:event.target.value})}/>;}
    const view=render(<Harness id="first-export-fixture"/>);fireEvent.change(screen.getByRole('textbox'),{target:{value:'Earlier output'}});const earlier=scratch!,captured=earlier.value;
    fireEvent.change(screen.getByRole('textbox'),{target:{value:'Newer output'}});view.rerender(<Harness id="second-export-fixture"/>);await act(async()=>{});earlier.accepted(captured);
    expect((screen.getByRole('textbox') as HTMLInputElement).value).toBe('');view.rerender(<Harness id="first-export-fixture"/>);await act(async()=>{});expect((screen.getByRole('textbox') as HTMLInputElement).value).toBe('Newer output');expect(scratch!.dirty).toBe(true);
    await act(async()=>scratch!.discard());view.unmount();
  });
  it('preserves an unsaved workspace name across settings unmount until explicit discard',async()=>{
    const first=render(<AppearanceSettings settings={DEFAULT_SETTINGS} onChange={async()=>{}}/>);fireEvent.change(screen.getByRole('textbox',{name:'Workspace name'}),{target:{value:'Synthetic workspace'}});first.unmount();
    const second=render(<AppearanceSettings settings={DEFAULT_SETTINGS} onChange={async()=>{}}/>);expect((screen.getByRole('textbox',{name:'Workspace name'}) as HTMLInputElement).value).toBe('Synthetic workspace');await act(async()=>fireEvent.click(screen.getByRole('button',{name:'Discard name'})));second.unmount();
  });
});
