import { useRef, useState } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import App from '../App';
import { DEFAULT_SETTINGS } from '../types';
import { DEFAULT_LAYOUT, useWorkspaceLayout } from '../workspaceLayout';
import { updateReadiness } from '../updateReadiness';
import { requestUpdateRestart } from '../updateHandoff';

const deferred=<T,>()=>{let resolve!:(value:T)=>void;const promise=new Promise<T>(done=>{resolve=done;});return {promise,resolve};};
const attempt='a'.repeat(32),receipt='d'.repeat(64);
const project={id:'fresh-project',name:'Fresh project',path:'/fixture/fresh'};
const chat={id:'fresh-chat',title:'Fresh conversation',project_id:project.id,model:'fresh-model',messages:[{role:'assistant',content:'Fresh native answer'}]};
const fresh={settings:{...DEFAULT_SETTINGS,model:'fresh-model',ui_appearance:{name:'Fresh workspace',accent:'copper',motion:'system'}},projects:[project],chats:[chat],runs:[],capabilities:{}};
function response(value:unknown,ok=true){return {ok,status:ok?200:401,json:async()=>value} as Response;}
function result(action:string,data:Record<string,unknown>={}){
  if(action==='bootstrap')return fresh;if(action==='get_chat')return chat;if(action==='setup_status')return {first_run:false};
  if(action==='ui_layout_get')return {...DEFAULT_LAYOUT,sidebar_width:310};if(action==='ui_layout_save')return data.layout;
  if(action==='draft_get')return {revision:1,content:data.chat_id===chat.id?{text:'Fresh native draft',document_ids:[],skills:[],space_ids:[]}: {},scope_available:true};
  if(action==='draft_save')return {revision:2,content:data.content,scope_available:true};if(action==='draft_confirm')return {ok:true,receipt};
  if(action==='draft_clear')return {revision:3,content:{},scope_available:true};
  if(action==='update_handoff_begin')return {attempt_id:attempt,challenge:'b'.repeat(64)};if(action==='update_handoff_abort')return {attempt_id:attempt,aborted:true,not_executed:true};
  if(action==='update_handoff_ack')return {phase:'prepared',confirmation:'c'.repeat(64)};if(action==='update_handoff_commit')return {attempt_id:attempt,phase:'committed'};
  if(action==='host_status')return {visible:true,visibility_seq:0};if(action==='models')return {models:[{name:'fresh-model'}]};if(action==='projects')return {projects:[project]};if(action==='chats')return {chats:[chat]};
  if(['runs','schedules','spaces'].includes(action))return {[action]:[]};if(action==='pending_questions')return {questions:[]};return {};
}
const layoutObservation=()=>updateReadiness.diagnose().participants.find(row=>row.kind==='layout');
afterEach(()=>{updateReadiness.aborted(attempt);updateReadiness.dismiss();delete window.pywebview;vi.unstubAllGlobals();});

describe('committed bootstrap readiness',()=>{
  it.each(['migrated-model',''])('opens legacy history without restoring an unavailable engine name (%s)',async mapped=>{
    const historical={...chat,model:'old-ollama-name:latest',composer_provider_id:'forge-native',composer_model:mapped};
    const call=vi.fn(async(action:string,data:Record<string,unknown>={})=>{
      if(action==='bootstrap')return {...fresh,settings:{...fresh.settings,provider_id:'forge-native',model:'current-native'}};
      if(action==='get_chat')return historical;
      if(action==='models')return {models:[{name:'current-native',display_name:'Current native model'},{name:'migrated-model',display_name:'Imported native model'}]};
      return result(action,data);
    });window.pywebview={api:{call}};render(<App/>);await act(async()=>window.dispatchEvent(new Event('pywebviewready')));
    await screen.findByText('Fresh native answer');await screen.findByRole('button',{name:mapped?'Imported native model':'Current native model'});
    expect(screen.queryByRole('button',{name:/old-ollama-name/})).toBeNull();
  });
  it('reproduces ref-only activation losing its effect after React bails out an unchanged state batch',async()=>{
    let activate!:()=>void,rerender!:()=>void;const call=vi.fn(async()=>DEFAULT_LAYOUT);window.pywebview={api:{call}};
    function Owner(){const booted=useRef(false),[cycle,setCycle]=useState(false),[tick,setTick]=useState(0);const layout=useWorkspaceLayout(booted.current,()=>{});activate=()=>{booted.current=true;setCycle(true);setCycle(false);};rerender=()=>setTick(value=>value+1);return <span>{String(cycle)}/{tick}/{String(layout.ready)}</span>;}
    render(<Owner/>);await act(async()=>activate());expect(layoutObservation()).toEqual({kind:'layout',reasons:['preferences_pending'],state:'uninitialized',load_started:false});expect(call).not.toHaveBeenCalled();
    await act(async()=>rerender());expect(screen.getByText('false/1/false')).toBeTruthy();expect(call).not.toHaveBeenCalled();expect(layoutObservation()!.state).toBe('uninitialized');
  });
  it('initializes the real layout hook when readiness uses React state in the same batch',async()=>{
    let activate!:()=>void;const call=vi.fn(async()=>DEFAULT_LAYOUT);window.pywebview={api:{call}};
    function Owner(){const [ready,setReady]=useState(false),[cycle,setCycle]=useState(false);const layout=useWorkspaceLayout(ready,()=>{});activate=()=>{setReady(true);setCycle(true);setCycle(false);};return <span>{String(cycle)}/{String(layout.ready)}</span>;}
    render(<Owner/>);await act(async()=>activate());expect(call).toHaveBeenCalledWith('ui_layout_get',{client_id:'desktop'});expect(screen.getByText('false/true')).toBeTruthy();expect(layoutObservation()!.state).toBe('ready');
  });
  it('cannot send, restore preferences, or acknowledge restart while bootstrap is still pending',async()=>{
    const bootstrap=deferred<unknown>(),call=vi.fn(async(action:string,data:Record<string,unknown>={})=>action==='bootstrap'?bootstrap.promise:result(action,data));window.pywebview={api:{call}};
    render(<App/>);await act(async()=>window.dispatchEvent(new Event('pywebviewready')));await waitFor(()=>expect(call.mock.calls.some(([action])=>action==='bootstrap')).toBe(true));
    fireEvent.change(screen.getByRole('textbox',{name:'Message Forge'}),{target:{value:'/help'}});expect(screen.getByRole('button',{name:'Send message'})).toHaveProperty('disabled',true);expect(screen.getByRole('button',{name:'Discard draft'})).toHaveProperty('disabled',true);fireEvent.click(screen.getByRole('button',{name:'Discard draft'}));expect((screen.getByRole('textbox',{name:'Message Forge'}) as HTMLTextAreaElement).value).toBe('/help');fireEvent.keyDown(screen.getByRole('textbox',{name:'Message Forge'}),{key:'Enter'});
    await act(async()=>requestUpdateRestart());expect(updateReadiness.getSnapshot().reason).toBe('workspace_loading');expect(call.mock.calls.some(([action])=>['start_chat','command','draft_get','ui_layout_get','update_handoff_ack','update_handoff_commit'].includes(action))).toBe(false);
    await act(async()=>bootstrap.resolve({...fresh,chats:[]}));await waitFor(()=>expect(call).toHaveBeenCalledWith('ui_layout_get',{client_id:'desktop'}));await waitFor(()=>expect(layoutObservation()!.state).toBe('ready'));expect(screen.getByRole('button',{name:'Send message'})).toHaveProperty('disabled',false);expect(updateReadiness.diagnose().participants.find(row=>row.kind==='workspace')!.state).toBe('ready');
  });
  it('keeps a failed current bootstrap blocked without activating saved-work owners',async()=>{
    const call=vi.fn(async(action:string,data:Record<string,unknown>={})=>action==='bootstrap'?{error:'Synthetic current coordinator unavailable'}:result(action,data));window.pywebview={api:{call}};
    render(<App/>);await act(async()=>window.dispatchEvent(new Event('pywebviewready')));await screen.findByText('Synthetic current coordinator unavailable');fireEvent.change(screen.getByRole('textbox',{name:'Message Forge'}),{target:{value:'/help'}});fireEvent.keyDown(screen.getByRole('textbox',{name:'Message Forge'}),{key:'Enter'});
    expect(screen.getByRole('button',{name:'Send message'})).toHaveProperty('disabled',true);await act(async()=>requestUpdateRestart());expect(updateReadiness.getSnapshot().reason).toBe('workspace_loading');expect(layoutObservation()!.state).toBe('disabled');expect(call.mock.calls.some(([action])=>['draft_get','ui_layout_get','start_chat','command','update_handoff_ack'].includes(action))).toBe(false);
  });
  it('lets a loaded workspace start a new chat while obsolete initial history is still pending',async()=>{
    const initialChat=deferred<unknown>(),call=vi.fn(async(action:string,data:Record<string,unknown>={})=>action==='get_chat'?initialChat.promise:result(action,data));window.pywebview={api:{call}};
    render(<App/>);await act(async()=>window.dispatchEvent(new Event('pywebviewready')));await waitFor(()=>expect(call.mock.calls.some(([action])=>action==='get_chat')).toBe(true));await waitFor(()=>expect(layoutObservation()!.state).toBe('ready'));
    fireEvent.click(screen.getByRole('button',{name:/New chat.*Ctrl N/}));fireEvent.change(screen.getByRole('textbox',{name:'Message Forge'}),{target:{value:'/help'}});expect(screen.getByRole('button',{name:'Send message'})).toHaveProperty('disabled',false);fireEvent.keyDown(screen.getByRole('textbox',{name:'Message Forge'}),{key:'Enter'});await waitFor(()=>expect(call.mock.calls.some(([action])=>action==='command')).toBe(true));
    await act(async()=>initialChat.resolve(chat));expect(screen.queryByText('Fresh native answer')).toBeNull();expect(updateReadiness.diagnose().participants.find(row=>row.kind==='workspace')!.state).toBe('ready');expect(call).toHaveBeenCalledWith('ui_layout_get',{client_id:'desktop'});expect(call.mock.calls.some(([action,data={}])=>action==='draft_get' && data.chat_id===chat.id)).toBe(false);
  });
  it('retains pre-bootstrap text in its original hidden scope and confirms it before restart',async()=>{
    const bootstrap=deferred<unknown>(),call=vi.fn(async(action:string,data:Record<string,unknown>={})=>action==='bootstrap'?bootstrap.promise:result(action,data));window.pywebview={api:{call}};
    render(<App/>);await act(async()=>window.dispatchEvent(new Event('pywebviewready')));fireEvent.change(screen.getByRole('textbox',{name:'Message Forge'}),{target:{value:'Retained startup fixture text'}});await act(async()=>bootstrap.resolve(fresh));await screen.findByText('Fresh native answer');await waitFor(()=>expect(layoutObservation()!.state).toBe('ready'));
    await waitFor(()=>expect(call.mock.calls.some(([action,data={}])=>action==='draft_save' && data.scope_kind==='new_chat' && data.project_id===null && (data.content as {text?:string})?.text==='Retained startup fixture text')).toBe(true));
    await act(async()=>requestUpdateRestart());expect(call.mock.calls.some(([action,data={}])=>action==='draft_confirm' && data.scope_kind==='new_chat' && data.project_id===null)).toBe(true);expect(call.mock.calls.some(([action])=>action==='update_handoff_ack')).toBe(true);expect((screen.getByRole('textbox',{name:'Message Forge'}) as HTMLTextAreaElement).value).toBe('Fresh native draft');
  });
  it.each(['success','error'])('ignores a superseded bootstrap %s on the same native bridge',async outcome=>{
    const old=deferred<unknown>();let starts=0;const call=vi.fn(async(action:string,data:Record<string,unknown>={})=>action==='bootstrap' && ++starts===1?old.promise:result(action,data));window.pywebview={api:{call}};
    render(<App/>);await act(async()=>window.dispatchEvent(new Event('pywebviewready')));await waitFor(()=>expect(starts).toBe(1));await act(async()=>window.dispatchEvent(new Event('pywebviewready')));await screen.findByText('Fresh native answer');await waitFor(()=>expect(layoutObservation()!.state).toBe('ready'));
    await act(async()=>old.resolve(outcome==='success'?{...fresh,settings:{...DEFAULT_SETTINGS,model:'superseded-model'},projects:[],runs:[{id:'superseded-run',status:'running'}]}:{error:'Authenticate synthetic superseded coordinator'}));expect(screen.getByText('Fresh native answer')).toBeTruthy();expect(screen.queryByText('Authenticate synthetic superseded coordinator')).toBeNull();expect(updateReadiness.diagnose().participants.find(row=>row.kind==='workspace')!.state).toBe('ready');expect(layoutObservation()!.state).toBe('ready');expect(call.mock.calls.filter(([action])=>action==='ui_layout_get')).toHaveLength(1);
  });
  it.each(['success','error'])('ignores an old HTTP bootstrap %s after accepting the native workspace',async outcome=>{
    const oldBootstrap=deferred<Response>();vi.stubGlobal('fetch',vi.fn(async(url:string,request:RequestInit)=>url.endsWith('/bootstrap')?oldBootstrap.promise:response(result(url.split('/').pop()!,JSON.parse(String(request.body))))));
    render(<App/>);await act(async()=>window.dispatchEvent(new Event('pywebviewready')));await waitFor(()=>expect(fetch).toHaveBeenCalledWith('/api/v1/bootstrap',expect.anything()));
    const call=vi.fn(async(action:string,data:Record<string,unknown>={})=>result(action,data));window.pywebview={api:{call}};await act(async()=>window.dispatchEvent(new Event('pywebviewready')));await screen.findByText('Fresh native answer');await waitFor(()=>expect((screen.getByRole('textbox',{name:'Message Forge'}) as HTMLTextAreaElement).value).toBe('Fresh native draft'));
    const stale={settings:{...DEFAULT_SETTINGS,model:'stale-model',ui_appearance:{name:'Stale workspace',accent:'rose',motion:'system'}},projects:[{id:'stale-project',name:'Stale project',path:'/fixture/stale'}],chats:[{...chat,id:'stale-chat'}],runs:[{id:'stale-run',status:'running'}],capabilities:{}};
    await act(async()=>oldBootstrap.resolve(outcome==='success'?response(stale):response({error:'Authenticate synthetic stale browser'},false)));
    expect(screen.getByText('Fresh native answer')).toBeTruthy();expect(screen.queryByText('Stale project')).toBeNull();expect(screen.queryByText('Stale workspace')).toBeNull();expect(screen.queryByRole('heading',{name:'Connect to your coordinator'})).toBeNull();expect(screen.queryByText('Authenticate synthetic stale browser')).toBeNull();expect(screen.getByRole('button',{name:'Send message'})).toHaveProperty('disabled',false);expect(layoutObservation()!.state).toBe('ready');expect(updateReadiness.diagnose().participants.find(row=>row.kind==='workspace')!.state).toBe('ready');
    expect(call.mock.calls.filter(([action])=>action==='ui_layout_get')).toEqual([['ui_layout_get',{client_id:'desktop'}]]);expect(screen.getByRole('separator',{name:'Resize sidebar'}).getAttribute('aria-valuenow')).toBe('310');
  });
  it('supersedes old automatic chat restoration and setup replies while preserving fresh native history',async()=>{
    const oldChat=deferred<Response>(),oldSetup=deferred<Response>();const old={...chat,id:'old-chat',title:'Old conversation',messages:[{role:'assistant',content:'Old browser answer'}]};
    vi.stubGlobal('fetch',vi.fn(async(url:string,request:RequestInit)=>url.endsWith('/bootstrap')?response({...fresh,chats:[old]}):url.endsWith('/get_chat')?oldChat.promise:url.endsWith('/setup_status')?oldSetup.promise:response(result(url.split('/').pop()!,JSON.parse(String(request.body))))));
    render(<App/>);await act(async()=>window.dispatchEvent(new Event('pywebviewready')));await waitFor(()=>expect(fetch).toHaveBeenCalledWith('/api/v1/get_chat',expect.anything()));
    const call=vi.fn(async(action:string,data:Record<string,unknown>={})=>result(action,data));window.pywebview={api:{call}};await act(async()=>window.dispatchEvent(new Event('pywebviewready')));await screen.findByText('Fresh native answer');
    await act(async()=>{oldChat.resolve(response(old));oldSetup.resolve(response({first_run:true}));});expect(screen.queryByText('Old browser answer')).toBeNull();expect(screen.getByText('Fresh native answer')).toBeTruthy();expect(screen.queryByRole('heading',{name:'Welcome to Forge'})).toBeNull();expect(call.mock.calls.filter(([action])=>action==='get_chat')).toEqual([['get_chat',{id:chat.id,limit:80}]]);expect(layoutObservation()!.state).toBe('ready');
  });
});
