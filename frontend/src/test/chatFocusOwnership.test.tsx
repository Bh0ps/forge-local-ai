import { useRef, useState } from 'react';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import App from '../App';
import { useChatActionFocus } from '../chatActionFocus';
import { DEFAULT_SETTINGS } from '../types';
import { DEFAULT_LAYOUT } from '../workspaceLayout';
import { updateReadiness } from '../updateReadiness';

const attempt = 'c'.repeat(32);
function deferred() { let resolve!:(value:unknown)=>void, reject!:(value:unknown)=>void; const promise=new Promise((done,failed)=>{resolve=done;reject=failed;});return {promise,resolve,reject}; }
beforeEach(()=>vi.spyOn(document,'hasFocus').mockReturnValue(true));
afterEach(()=>{cleanup();updateReadiness.aborted(attempt);updateReadiness.dismiss();vi.restoreAllMocks();});

async function workspace(active=false) {
  const pending=deferred(), first={id:'first',project_id:null,title:'First conversation',messages:[{id:81,role:'user',content:'Current request'},{id:82,role:'assistant',content:'Current answer'}],total_messages:active?2:240};
  const other={id:'other',project_id:null,title:'Other conversation',messages:[{id:1,role:'assistant',content:'Other saved answer'}],total_messages:1};
  const run={id:'original-run',chat_id:'first',status:'running',created_at:'2026-01-01T00:00:00Z',request_message_id:81};
  const drafts=new Map<string,{revision:number;content:Record<string,unknown>;scope_available:boolean}>();
  const call=vi.fn(async(action:string,data:Record<string,unknown>={})=>{
    if(action==='bootstrap')return {settings:{...DEFAULT_SETTINGS,model:'fixture'},projects:[],chats:[first,other],runs:active?[run]:[],capabilities:{}};
    if(action==='get_chat')return data.id==='other'?other:Number(data.limit)>80?pending.promise:first;
    if(action==='run_steer')return pending.promise;
    if(action==='poll')return {events:[],status:'running',finished:false,next_cursor:0};
    if(action==='setup_status')return {first_run:false};
    if(action==='host_status')return {visible:true,visibility_seq:0};
    if(action==='ui_layout_get')return DEFAULT_LAYOUT;
    if(action==='ui_layout_save')return data.layout;
    if(action==='pending_questions')return {questions:[]};
    if(action==='show')return {capabilities:[],model_info:{}};
    if(['models','projects','chats','runs','spaces','schedules','skills'].includes(action))return {[action]:action==='chats'?[first,other]:action==='runs'&&active?[run]:[]};
    const key=JSON.stringify([data.scope_kind,data.chat_id,data.project_id]);
    let current=drafts.get(key);
    if(!current){current={revision:0,content:{text:data.chat_id==='first'?(active?'Original direction':'Retained reading draft'):data.chat_id==='other'?'Other retained draft':''},scope_available:true};drafts.set(key,current);}
    if(action==='draft_get')return current;
    if(action==='draft_save'){
      if(data.expected_revision!==current.revision)throw Error('Synthetic draft revision mismatch');
      const next={revision:current.revision+1,content:data.content as Record<string,unknown>,scope_available:true};drafts.set(key,next);return next;
    }
    throw Error('Unexpected synthetic chat focus action');
  });
  window.pywebview={api:{call}};
  const view=render(<App/>);await act(async()=>window.dispatchEvent(new Event('pywebviewready')));
  await screen.findByText('Current answer');const prompt=screen.getByRole('textbox',{name:'Message Forge'}) as HTMLTextAreaElement;
  await waitFor(()=>expect(prompt.value).toBe(active?'Original direction':'Retained reading draft'));
  return {pending,first,call,prompt,view};
}
function disabledBlurToBody(trigger:HTMLButtonElement) {
  expect(trigger.disabled).toBe(true);
  // Chromium retires a focused disabled button to body; jsdom does not do so.
  act(()=>{document.body.tabIndex=-1;document.body.focus();document.body.removeAttribute('tabindex');});
}
async function startSteer(owner:Awaited<ReturnType<typeof workspace>>, fromPrompt=false) {
  const trigger=screen.getByRole('button',{name:'Steer'}) as HTMLButtonElement;
  (fromPrompt?owner.prompt:trigger).focus();await userEvent.keyboard('{Enter}');
  await waitFor(()=>expect(owner.call.mock.calls.filter(([action])=>action==='run_steer')).toHaveLength(1));
  await waitFor(()=>expect(trigger.disabled).toBe(true));
  if(!fromPrompt)disabledBlurToBody(trigger);
  return trigger;
}
async function startHistory(owner:Awaited<ReturnType<typeof workspace>>) {
  const trigger=screen.getByRole('button',{name:'Load earlier messages'}) as HTMLButtonElement;
  trigger.focus();await userEvent.keyboard('{Enter}');
  await waitFor(()=>expect(trigger.disabled).toBe(true));
  disabledBlurToBody(trigger);
  expect(document.activeElement).toBe(document.body);return trigger;
}

describe('actual App steering reply owns only its original focus',()=>{
  it('returns to the original composer after its focused submission button becomes disabled',async()=>{
    const owner=await workspace(true);await startSteer(owner);const focus=vi.spyOn(owner.prompt,'focus');
    await act(async()=>owner.pending.resolve({ok:true}));await waitFor(()=>expect(owner.prompt.value).toBe(''));
    expect(document.activeElement).toBe(owner.prompt);expect(focus).toHaveBeenCalledWith({preventScroll:true});
    const requests=owner.call.mock.calls.filter(([action])=>action==='run_steer');expect(requests).toHaveLength(1);
    expect(requests[0][1]).toMatchObject({run_id:'original-run',text:'Original direction'});expect(typeof requests[0][1]?.client_id).toBe('string');
  });
  it('keeps normal Enter steering in the original composer without extra actions',async()=>{
    const owner=await workspace(true);await startSteer(owner,true);await act(async()=>owner.pending.resolve({ok:true}));
    expect(document.activeElement).toBe(owner.prompt);expect(owner.call.mock.calls.filter(([action])=>action==='run_steer')).toHaveLength(1);
  });
  it.each(['success','failure'])('preserves another chat draft and Context focus after a delayed %s reply',async result=>{
    const owner=await workspace(true);await startSteer(owner);
    await userEvent.click(screen.getByRole('button',{name:'Other conversation'}));await screen.findByText('Other saved answer');
    await waitFor(()=>expect(owner.prompt.value).toBe('Other retained draft'));
    fireEvent.change(owner.prompt,{target:{value:'Keep the other draft'}});
    const control=screen.getByRole('button',{name:/^Context controls/});control.focus();
    await act(async()=>result==='success'?owner.pending.resolve({ok:true}):owner.pending.reject(Error('Synthetic steering failure')));
    expect(document.activeElement).toBe(control);expect(owner.prompt.value).toBe('Keep the other draft');
    expect(owner.call.mock.calls.filter(([action])=>action==='run_steer')).toHaveLength(1);
  });
  it('retains a newer same-chat draft and does not move focus after later input',async()=>{
    const owner=await workspace(true);await startSteer(owner);owner.prompt.focus();fireEvent.change(owner.prompt,{target:{value:'A newer direction'}});
    const control=screen.getByRole('button',{name:'Notifications'});control.focus();await act(async()=>owner.pending.resolve({ok:true}));
    expect(owner.prompt.value).toBe('A newer direction');expect(document.activeElement).toBe(control);
  });
  it('does not refocus the composer when later typing retains the same focused input',async()=>{
    const owner=await workspace(true);await startSteer(owner,true);
    fireEvent.change(owner.prompt,{target:{value:'Typed after submission'}});const focus=vi.spyOn(owner.prompt,'focus');
    await act(async()=>owner.pending.resolve({ok:true}));expect(focus).not.toHaveBeenCalled();
    expect(document.activeElement).toBe(owner.prompt);expect(owner.prompt.value).toBe('Typed after submission');
  });
});

describe('actual App pagination restores reader focus after commit',()=>{
  it.each(['remaining','final','failure'])('restores the owned %s-page reader with preventScroll and no composer focus',async outcome=>{
    const owner=await workspace(),trigger=await startHistory(owner),history=screen.getByRole('region',{name:'Conversation history'});
    const triggerFocus=vi.spyOn(trigger,'focus'),regionFocus=vi.spyOn(history,'focus'),composerFocus=vi.spyOn(owner.prompt,'focus');
    await act(async()=>outcome==='failure'?owner.pending.reject(Error('Synthetic history failure')):owner.pending.resolve({...owner.first,messages:[{id:1,role:'assistant',content:'Earlier revealed answer'},...owner.first.messages],total_messages:outcome==='final'?3:240}));
    if(outcome==='final')await waitFor(()=>expect(trigger.isConnected).toBe(false));else await waitFor(()=>expect(trigger.disabled).toBe(false));
    if(outcome==='final'){expect(trigger.isConnected).toBe(false);expect(document.activeElement).toBe(history);expect(regionFocus).toHaveBeenCalledWith({preventScroll:true});expect(history.getAttribute('tabindex')).toBe('-1');}
    else{expect(document.activeElement).toBe(trigger);expect(triggerFocus).toHaveBeenCalledWith({preventScroll:true});}
    expect(composerFocus).not.toHaveBeenCalled();expect(owner.prompt.value).toBe('Retained reading draft');
    expect(owner.call.mock.calls.filter(([action,data])=>action==='get_chat'&&Number(data?.limit)>80)).toEqual([['get_chat',{id:'first',limit:160}]]);
  });
  it('lets another focused reader control win while history arrives',async()=>{
    const owner=await workspace();await startHistory(owner);const control=screen.getByRole('button',{name:'Notifications'});control.focus();
    await act(async()=>owner.pending.resolve({...owner.first,total_messages:2}));expect(document.activeElement).toBe(control);expect(owner.prompt.value).toBe('Retained reading draft');
  });
  it('does not claim pointer/programmatic activation from an unfocused history button',async()=>{
    const owner=await workspace(),control=screen.getByRole('button',{name:'Notifications'});control.focus();fireEvent.click(screen.getByRole('button',{name:'Load earlier messages'}));
    await act(async()=>owner.pending.resolve({...owner.first,total_messages:2}));expect(document.activeElement).toBe(control);
  });
  it('leaves a newer chat and its focused control intact when the older page arrives',async()=>{
    const owner=await workspace();await startHistory(owner);
    await userEvent.click(screen.getByRole('button',{name:'Other conversation'}));await screen.findByText('Other saved answer');
    await waitFor(()=>expect(owner.prompt.value).toBe('Other retained draft'));
    const control=screen.getByRole('button',{name:/^Context controls/});control.focus();
    await act(async()=>owner.pending.resolve({...owner.first,total_messages:2}));
    expect(screen.getByText('Other saved answer')).toBeTruthy();expect(document.activeElement).toBe(control);expect(owner.prompt.value).toBe('Other retained draft');
  });
});

describe('pending chat focus cancellation is permanent',()=>{
  for(const action of ['steer','history'] as const)it.each(['native-hide-show','document-hide-show','lock-unlock','bridge','wheel','navigation','blur','pointerdown','keydown','input','change','scroll'])(`${action} refuses later focus after %s`,async boundary=>{
    const owner=await workspace(action==='steer');if(action==='steer')await startSteer(owner);else await startHistory(owner);
    if(boundary==='native-hide-show'){
      fireEvent(window,new CustomEvent('forge:visibility',{detail:{visible:false,seq:10}}));fireEvent(window,new CustomEvent('forge:visibility',{detail:{visible:true,seq:11}}));
    }else if(boundary==='document-hide-show'){
      const state=vi.spyOn(document,'visibilityState','get').mockReturnValue('hidden');fireEvent(document,new Event('visibilitychange'));state.mockReturnValue('visible');fireEvent(document,new Event('visibilitychange'));
    }else if(boundary==='lock-unlock'){act(()=>{expect(updateReadiness.start(attempt,()=>{})).toBe(true);updateReadiness.aborted(attempt);});}
    else if(boundary==='bridge'){window.pywebview={api:{call:owner.call}};fireEvent(window,new Event('pywebviewready'));}
    else if(['wheel','pointerdown','keydown','input','change','scroll'].includes(boundary))fireEvent(document,new Event(boundary,{bubbles:true}));
    else if(boundary==='blur')fireEvent(window,new Event('blur'));
    else fireEvent(window,new Event('forge:navigate'));
    const focus=vi.spyOn(owner.prompt,'focus');await act(async()=>owner.pending.resolve(action==='steer'?{ok:true}:{...owner.first,total_messages:2}));
    expect(document.activeElement).toBe(document.body);expect(focus).not.toHaveBeenCalled();
    expect(owner.call.mock.calls.filter(([name,data])=>action==='steer'?name==='run_steer':name==='get_chat'&&Number(data?.limit)>80)).toHaveLength(1);
  });
  it.each(['steer','history'])('drops an unmounted %s owner before completing its original request',async action=>{
    const owner=await workspace(action==='steer');if(action==='steer')await startSteer(owner);else await startHistory(owner);
    const focus=vi.spyOn(owner.prompt,'focus');owner.view.unmount();await act(async()=>owner.pending.resolve(action==='steer'?{ok:true}:{...owner.first,total_messages:2}));expect(focus).not.toHaveBeenCalled();
  });
});

describe('focus scope cannot change and revert before settlement',()=>{
  it.each(['runId','projectId','chatId','navigation','chatGeneration','workspaceGeneration','hud','peek','transport','bridge'])('retires the exact request when %s changes',async field=>{
    const response=deferred();let change!:(value:Record<string,unknown>)=>void;
    function Harness(){
      const [changed,setChanged]=useState<Record<string,unknown>>({}),[busy,setBusy]=useState(false);change=setChanged;
      const destination=useRef<HTMLInputElement>(null),scope={page:'chat',chatId:'chat',projectId:null,runId:'run',navigation:1,chatGeneration:1,workspaceGeneration:1,hud:false,peek:false,transport:response,bridge:response,visible:true,...changed};
      const owner=useChatActionFocus(()=>scope);
      return <><button disabled={busy} onClick={async event=>{const request=owner.admit('steer',event.currentTarget);setBusy(true);await response.promise;setBusy(false);owner.complete(request,()=>destination.current);}}>Submit owned action</button><input ref={destination} aria-label="Owned destination"/></>;
    }
    render(<Harness/>);const trigger=screen.getByRole('button',{name:'Submit owned action'});trigger.focus();fireEvent.click(trigger);disabledBlurToBody(trigger as HTMLButtonElement);
    await act(async()=>change({[field]:['navigation','chatGeneration','workspaceGeneration'].includes(field)?2:field==='hud'||field==='peek'?true:field==='transport'||field==='bridge'?{}:'replacement'}));
    await act(async()=>change({}));await act(async()=>response.resolve({}));expect(document.activeElement).toBe(document.body);
  });
});
