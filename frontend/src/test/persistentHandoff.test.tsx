import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { useState } from 'react';
import { usePersistentDraft, type DraftContent } from '../persistentDraft';
import { updateReadiness } from '../updateReadiness';
const attempt='a'.repeat(32),receipt='d'.repeat(64);
let controller:ReturnType<typeof usePersistentDraft>;
function Harness(){const [text,setText]=useState('');controller=usePersistentDraft({scope_kind:'chat',project_id:'fixture',chat_id:'first'},{text},saved=>setText(saved.text || ''));return <><input aria-label="Draft" value={text} onChange={event=>{updateReadiness.changed();setText(event.target.value);}}/><button onClick={()=>void controller.reloadSaved()}>Reconcile</button></>;}
afterEach(()=>{updateReadiness.aborted(attempt);delete window.pywebview;});
describe('persistent draft confirmation ownership',()=>{
  it('confirms saved HTTP speech in bounded subsets without sending draft text or a restart grant',async()=>{
    const scope={scope_kind:'chat' as const,chat_id:'http-speech-fixture'},ids=Array.from({length:17},(_,index)=>index.toString(16).padStart(64,'0'));const calls:{action:string;data:Record<string,unknown>}[]=[];
    vi.stubGlobal('fetch',vi.fn(async(url:string,request:{body:string})=>{const action=url.split('/').pop()!,data=JSON.parse(request.body);calls.push({action,data});return {ok:true,json:async()=>action==='draft_get'?{revision:1,content:{text:'HTTP saved speech'},scope_available:true}:action==='draft_confirm'?{ok:true,confirmed:true}:{}};}));
    let owned:ReturnType<typeof usePersistentDraft>;function Speech(){const [text,setText]=useState('');owned=usePersistentDraft(scope,{text},saved=>setText(saved.text || ''));return <input aria-label="HTTP speech fixture" value={text}/>;}
    const view=render(<Speech/>);try{await waitFor(()=>expect((screen.getByRole('textbox') as HTMLInputElement).value).toBe('HTTP saved speech'));await act(async()=>ids.forEach(id=>owned!.trackDelivery(scope,id)));await act(async()=>owned!.flush());const confirmed=calls.filter(call=>call.action==='draft_confirm');expect(confirmed.map(call=>(call.data.delivery_ids as string[]).length)).toEqual([16,1]);expect(confirmed.flatMap(call=>call.data.delivery_ids)).toEqual(ids);expect(confirmed.every(call=>call.data.expected_revision===1 && !('content' in call.data) && !('attempt_id' in call.data))).toBe(true);}finally{view.unmount();vi.unstubAllGlobals();}
  });
  it('retires an acknowledged deleted scope and never revives it from a late restore or input producer',async()=>{
    let restore!:(value:unknown)=>void;const call=vi.fn(async(action:string,data:Record<string,unknown>)=>{
      if(action==='draft_get' && data.chat_id==='deleted-fixture')return new Promise(resolve=>{restore=resolve;});
      if(action==='draft_get')return {revision:0,content:{},scope_available:true};if(action==='draft_confirm'){if(data.chat_id==='deleted-fixture')return {error:'This draft scope is unavailable.'};return {ok:true,receipt};}return {};
    });window.pywebview={api:{call}};let owned:ReturnType<typeof usePersistentDraft>;
    function Deletion({id}:{id:string}){const [text,setText]=useState('');owned=usePersistentDraft({scope_kind:'chat',chat_id:id},{text},saved=>setText(saved.text || ''));return <input aria-label="Deletion fixture" value={text} onChange={event=>setText(event.target.value)}/>;}
    const view=render(<Deletion id="deleted-fixture"/>);await waitFor(()=>expect(restore).toBeTruthy());await act(async()=>owned!.forgetChat('deleted-fixture'));view.rerender(<Deletion id="next-fixture"/>);await act(async()=>restore({revision:1,content:{text:'Deleted saved text'},scope_available:true}));
    await act(async()=>owned!.captureScope({scope_kind:'chat',chat_id:'deleted-fixture'},{text:'Late deleted transcript'}));owned!.trackDelivery({scope_kind:'chat',chat_id:'deleted-fixture'},'e'.repeat(64));await act(async()=>{});
    expect((screen.getByRole('textbox',{name:'Deletion fixture'}) as HTMLInputElement).value).toBe('');updateReadiness.start(attempt,vi.fn());expect((await updateReadiness.confirm(attempt)).ok).toBe(true);
    expect(call.mock.calls.filter(([name])=>name==='draft_confirm').map(([,data])=>data.chat_id)).toEqual(['next-fixture']);expect(call.mock.calls.some(([name,data])=>name==='draft_save' && data.chat_id==='deleted-fixture')).toBe(false);
  });
  it('does not confirm a speech delivery against an older saved revision before the appended text CAS completes',async()=>{
    let accept!:(value:unknown)=>void,revision=1;const scope={scope_kind:'chat' as const,chat_id:'speech-order-fixture'};const delivery='e'.repeat(64);
    const call=vi.fn(async(action:string,data:Record<string,unknown>)=>{
      if(action==='draft_get')return {revision,content:{text:'Earlier saved text'},scope_available:true};
      if(action==='draft_save')return new Promise(resolve=>{accept=resolve;});if(action==='draft_confirm')return {ok:true,confirmed:true};return {};
    });window.pywebview={api:{call}};let owned:ReturnType<typeof usePersistentDraft>;function Speech(){const [text,setText]=useState('');owned=usePersistentDraft(scope,{text},saved=>setText(saved.text || ''));return <input aria-label="Speech order fixture" value={text} onChange={event=>setText(event.target.value)}/>;}
    render(<Speech/>);await waitFor(()=>expect((screen.getByRole('textbox') as HTMLInputElement).value).toBe('Earlier saved text'));
    await act(async()=>{fireEvent.change(screen.getByRole('textbox'),{target:{value:'Earlier saved text New speech'}});owned!.captureScope(scope,{text:'Earlier saved text New speech'});owned!.trackDelivery(scope,delivery);});await waitFor(()=>expect(accept).toBeTruthy());expect(call.mock.calls.some(([name])=>name==='draft_confirm')).toBe(false);
    await act(async()=>accept({revision:++revision,content:{text:'Earlier saved text New speech'},scope_available:true}));await waitFor(()=>expect(call).toHaveBeenCalledWith('draft_confirm',{...scope,expected_revision:2,delivery_ids:[delivery]}));
    const confirming=call.mock.calls.find(([name])=>name==='draft_confirm')![1];expect(confirming).not.toHaveProperty('attempt_id');expect(confirming).not.toHaveProperty('content');
  });
  it('keeps delivery IDs after an unconfirmed ordinary save and retries the same saved revision for readiness',async()=>{
    let revision=1;const scope={scope_kind:'chat' as const,chat_id:'speech-retry-fixture'},delivery='f'.repeat(64);const call=vi.fn(async(action:string,data:Record<string,unknown>)=>{
      if(action==='draft_get')return {revision,content:{text:'Saved speech'},scope_available:true};if(action==='draft_save')return {revision:++revision,content:data.content,scope_available:true};if(action==='draft_confirm')return data.attempt_id ? {ok:true,receipt} : {error:'Synthetic confirmation unavailable'};return {};
    });window.pywebview={api:{call}};let owned:ReturnType<typeof usePersistentDraft>;function Speech(){const [text,setText]=useState('');owned=usePersistentDraft(scope,{text},saved=>setText(saved.text || ''));return <input aria-label="Speech retry fixture" value={text}/>;}
    render(<Speech/>);await waitFor(()=>expect((screen.getByRole('textbox') as HTMLInputElement).value).toBe('Saved speech'));await act(async()=>owned!.trackDelivery(scope,delivery));await act(async()=>owned!.flush());
    updateReadiness.start(attempt,vi.fn());expect((await updateReadiness.confirm(attempt)).ok).toBe(true);expect(call.mock.calls.find(([name,data])=>name==='draft_confirm' && data.attempt_id)?.[1]).toMatchObject({...scope,expected_revision:1,delivery_ids:[delivery],attempt_id:attempt});expect((screen.getByRole('textbox') as HTMLInputElement).value).toBe('Saved speech');
  });
  it('reopens the same retained private owner and reconciles its failed save without losing local work',async()=>{
    let fail=true,revision=0,stored:DraftContent={};const call=vi.fn(async(action:string,data:Record<string,unknown>)=>{
      if(action==='draft_get')return {revision,content:stored,scope_available:true};if(action==='draft_save'){if(fail)return {error:'Draft changed in another window'};stored=data.content as DraftContent;return {revision:++revision,content:stored,scope_available:true};}if(action==='draft_confirm')return {ok:true,receipt};return {};
    });window.pywebview={api:{call}};let value:ReturnType<typeof usePersistentDraft>;function Reopened(){const [text,setText]=useState('');value=usePersistentDraft({scope_kind:'chat',chat_id:'retained-owner'},{text},saved=>setText(saved.text || ''),true,'retained-test-owner');return <input aria-label="Retained fixture" value={text} onChange={event=>setText(event.target.value)}/>;}
    const first=render(<Reopened/>);await act(async()=>{});fireEvent.change(screen.getByRole('textbox',{name:'Retained fixture'}),{target:{value:'Retained local work'}});await act(async()=>value!.flush());first.unmount();const second=render(<Reopened/>);await act(async()=>{});
    expect((screen.getByRole('textbox',{name:'Retained fixture'}) as HTMLInputElement).value).toBe('Retained local work');expect(value!.conflict).toBe(true);updateReadiness.start(attempt,vi.fn());expect(await updateReadiness.confirm(attempt)).toEqual({ok:false,reason:'draft_conflict'});updateReadiness.aborted(attempt);
    fail=false;await act(async()=>value!.reloadSaved());await act(async()=>{});updateReadiness.start(attempt,vi.fn());expect((await updateReadiness.confirm(attempt)).ok).toBe(true);updateReadiness.aborted(attempt);second.unmount();
  });
  it('confirms the latest visible and hidden scopes, keeps typed failures, and retains an unmounted queue',async()=>{
    const saved=new Map<string,{revision:number;content:DraftContent}>();let failed=false;let deferredSave:((value:unknown)=>void)|undefined;
    const call=vi.fn(async(action:string,data:Record<string,unknown>)=>{
      const key=String(data.chat_id),record=saved.get(key)||{revision:0,content:{}};
      if(action==='draft_get')return {...record,scope_available:true};
      if(action==='draft_confirm')return {ok:true,receipt};
      if(action==='draft_save'){
        if(failed)return {error:'Synthetic save unavailable'};
        if(data.expected_revision!==record.revision)return {error:'Draft changed in another window'};
        const next={revision:record.revision+1,content:data.content as DraftContent};saved.set(key,next);
        if(key==='hidden' && deferredSave===undefined && (data.content as DraftContent).text==='Later hidden text')return new Promise(resolve=>{deferredSave=resolve;});
        return {...next,scope_available:true};
      }
      return {};
    });window.pywebview={api:{call}};const view=render(<Harness/>);await waitFor(()=>expect(call.mock.calls.some(([name])=>name==='draft_get')).toBe(true));await act(async()=>{});
    fireEvent.change(screen.getByRole('textbox',{name:'Draft'}),{target:{value:'Current fixture text'}});
    await act(async()=>controller.captureScope({scope_kind:'chat',project_id:'fixture',chat_id:'hidden'},{text:'Hidden fixture text'}));
    await act(async()=>controller.flush());controller.trackDelivery({scope_kind:'chat',project_id:'fixture',chat_id:'hidden'},'e'.repeat(64));updateReadiness.start(attempt,vi.fn());const first=await updateReadiness.confirm(attempt);expect(first.ok).toBe(true);
    expect(saved.get('first')?.content.text).toBe('Current fixture text');expect(saved.get('hidden')?.content.text).toBe('Hidden fixture text');
    expect(call.mock.calls.filter(([name,data])=>name==='draft_confirm' && (data as Record<string,unknown>).attempt_id).map(([,data])=>(data as Record<string,unknown>).chat_id).sort()).toEqual(['first','hidden']);
    expect(call.mock.calls.find(([name,data])=>name==='draft_confirm' && (data as Record<string,unknown>).chat_id==='hidden' && (data as Record<string,unknown>).attempt_id)?.[1]).toMatchObject({delivery_ids:['e'.repeat(64)]});
    expect(call.mock.calls.filter(([name])=>name==='draft_confirm').every(([,data])=>!('content' in (data as object)))).toBe(true);updateReadiness.aborted(attempt);
    failed=true;fireEvent.change(screen.getByRole('textbox',{name:'Draft'}),{target:{value:'Keep failed local work'}});await act(async()=>controller.flush());updateReadiness.start(attempt,vi.fn());expect(await updateReadiness.confirm(attempt)).toEqual({ok:false,reason:'draft_save_failed'});expect((screen.getByRole('textbox',{name:'Draft'}) as HTMLInputElement).value).toBe('Keep failed local work');updateReadiness.aborted(attempt);
    failed=false;await act(async()=>controller.reloadSaved());await act(async()=>{});
    await act(async()=>controller.captureScope({scope_kind:'chat',project_id:'fixture',chat_id:'hidden'},{text:'Later hidden text'}));await waitFor(()=>expect(deferredSave).toBeTruthy());view.unmount();
    updateReadiness.start(attempt,vi.fn());let completed=false;const checking=updateReadiness.confirm(attempt).then(result=>{completed=true;return result;});await act(async()=>{});expect(completed).toBe(false);
    await act(async()=>deferredSave!({...saved.get('hidden'),scope_available:true}));const result=await checking;expect(result.ok).toBe(true);expect(saved.get('hidden')?.content.text).toBe('Later hidden text');
  });
});
