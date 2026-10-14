import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import App from '../App';
import { DEFAULT_SETTINGS } from '../types';
import { DEFAULT_LAYOUT } from '../workspaceLayout';
import { updateReadiness } from '../updateReadiness';

const deferred=<T,>()=>{let resolve!:(value:T)=>void;const promise=new Promise<T>(done=>{resolve=done;});return {promise,resolve};};
afterEach(()=>{cleanup();updateReadiness.dismiss();delete window.pywebview;});

it('restores the actual App Desktop preference before native Browser mounts',async()=>{
  const loading=deferred(),hidden=deferred();
  const chat={id:'view-pref-chat',title:'Synthetic conversation',project_id:null,messages:[{id:1,role:'user',content:'Synthetic request'}]};
  const drafts=new Map<string,{revision:number;content:unknown;scope_available:boolean}>();
  const call=vi.fn(async(action:string,data:Record<string,unknown>={})=>{
    if(action==='bootstrap')return {settings:DEFAULT_SETTINGS,projects:[],chats:[chat],runs:[],capabilities:{}};
    if(action==='get_chat')return chat;
    if(action==='setup_status')return {first_run:false};
    if(action==='ui_layout_get')return loading.promise;
    if(action==='ui_layout_save')return data.layout;
    if(action==='browser_native_hide')return hidden.promise;
    if(action.startsWith('browser_native_'))throw new Error('A restored Desktop view must not start the native browser');
    if(action==='computer_view_state')return {chat_id:chat.id,scope_run_id:null,mode:'shared_desktop',run_id:null,
      invocation_id:null,tool:null,status:'idle',action_count:0,action_count_truncated:false,evidence_mode:'none',snapshot:null,failure:null};
    const key=JSON.stringify([data.scope_kind,data.project_id,data.chat_id]);
    const current=drafts.get(key)||{revision:0,content:{},scope_available:true};
    if(action==='draft_get')return current;
    if(action==='draft_save'){
      if(data.expected_revision!==current.revision)return {conflict:true,draft:current};
      const saved={revision:current.revision+1,content:data.content,scope_available:true};drafts.set(key,saved);return saved;
    }
    if(action==='pending_questions')return {questions:[]};
    if(action==='host_status')return {visible:true,visibility_seq:0};
    if(['models','runs','projects','chats','schedules','spaces'].includes(action))return {[action]:action==='chats'?[chat]:[]};
    return {};
  });window.pywebview={api:{call}};
  render(<App/>);await act(async()=>window.dispatchEvent(new Event('pywebviewready')));
  await screen.findByText('Synthetic request');fireEvent(window,new Event('forge:browser-open'));
  await screen.findByText('Loading workspace view…');
  expect(call.mock.calls.some(([action])=>action==='browser_native_show' || action==='browser_native_status')).toBe(false);
  await act(async()=>loading.resolve({...DEFAULT_LAYOUT,right_panel_open:true,right_panel_tab:'browser',browser_mode:'desktop'}));
  await screen.findByText('Hiding the browser…');expect(screen.queryByRole('region',{name:'Desktop activity'})).toBeNull();
  await act(async()=>hidden.resolve({ok:true}));await screen.findByRole('region',{name:'Desktop activity'});
  expect(call.mock.calls.some(([action])=>action==='browser_native_show' || action==='browser_native_status')).toBe(false);
  await waitFor(()=>expect(call.mock.calls.filter(([action])=>action==='computer_view_state')).toEqual([['computer_view_state',{chat_id:chat.id}]]));
});
