import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import App from '../App';
import { DEFAULT_SETTINGS } from '../types';
import { DEFAULT_LAYOUT } from '../workspaceLayout';
import { requestUpdateRestart } from '../updateHandoff';
import { updateReadiness } from '../updateReadiness';
const attempt='a'.repeat(32),receipt='d'.repeat(64);
afterEach(()=>{updateReadiness.aborted(attempt);delete window.pywebview;});
describe('workspace restart fence',()=>{
  it('deletes a visited chat through the acknowledged action and ignores its late screenshot before restart',async()=>{
    const chat={id:'deleted-ui-fixture',title:'Delete fixture conversation',project_id:null,model:'fixture',messages:[{role:'assistant',content:'Delete fixture answer'}]};let deleted=false,finishCapture!:(value:unknown)=>void;
    const call=vi.fn(async(action:string,data:Record<string,unknown>={})=>{
      if(action==='bootstrap')return {settings:{...DEFAULT_SETTINGS,model:'fixture'},projects:[],chats:[chat],runs:[],capabilities:{}};
      if(action==='get_chat')return chat;if(action==='setup_status')return {first_run:false};if(action==='ui_layout_get')return DEFAULT_LAYOUT;if(action==='ui_layout_save')return data.layout;
      if(action==='chat_delete'){deleted=true;return {ok:true};}if(action==='draft_get')return {revision:0,content:{},scope_available:true};if(action==='draft_save')return {revision:1,content:data.content,scope_available:true};
      if(action==='draft_confirm')return data.chat_id===chat.id && deleted ? {error:'This draft scope is unavailable.'} : {ok:true,receipt};
      if(action==='update_handoff_begin')return {attempt_id:attempt,challenge:'b'.repeat(64)};if(action==='update_handoff_ack')return {phase:'prepared',confirmation:'c'.repeat(64)};if(action==='update_handoff_commit')return {attempt_id:attempt,phase:'committed'};if(action==='update_handoff_abort')return {aborted:true,not_executed:true};
      if(['projects','chats','runs','models','spaces','schedules','pending_questions'].includes(action))return {[action==='pending_questions'?'questions':action]:action==='chats'&&!deleted?[chat]:[]};return {};
    });window.pywebview={api:{call,screenshot:vi.fn(()=>new Promise(resolve=>{finishCapture=resolve;}))}};render(<App/>);await act(async()=>window.dispatchEvent(new Event('pywebviewready')));await screen.findByText('Delete fixture answer');
    fireEvent.change(screen.getByRole('textbox',{name:'Message Forge'}),{target:{value:'Explicitly deleted local draft'}});fireEvent.click(screen.getByRole('button',{name:'Attach screenshot'}));await waitFor(()=>expect(finishCapture).toBeTruthy());
    fireEvent.click(screen.getByRole('button',{name:'Chat actions for Delete fixture conversation'}));fireEvent.click(screen.getByRole('menuitem',{name:'Delete chat'}));fireEvent.click(screen.getByRole('button',{name:'Delete permanently'}));await waitFor(()=>expect(screen.queryByRole('dialog')).toBeNull());
    await act(async()=>finishCapture({image:'c3ludGhldGlj'}));await act(async()=>{});expect((screen.getByRole('textbox',{name:'Message Forge'}) as HTMLTextAreaElement).value).toBe('');expect(screen.queryByRole('img',{name:'Attachment 1'})).toBeNull();
    await act(async()=>requestUpdateRestart());expect(call.mock.calls.some(([name])=>name==='update_handoff_ack')).toBe(true);expect(call.mock.calls.some(([name,data])=>name==='draft_confirm' && data?.chat_id===chat.id)).toBe(false);
  });
  it('persists the appended native transcript before its ordinary delivery confirmation',async()=>{
    const chat={id:'saved-speech-fixture',title:'Saved speech fixture',project_id:null,model:'fixture',messages:[{role:'assistant',content:'Saved speech fixture answer'}]};let finishSpeech!:(value:unknown)=>void,finishSave!:(value:unknown)=>void;const delivery='e'.repeat(64);
    const call=vi.fn(async(action:string,data:Record<string,unknown>={})=>{
      if(action==='bootstrap')return {settings:{...DEFAULT_SETTINGS,model:'fixture'},projects:[],chats:[chat],runs:[],capabilities:{}};if(action==='get_chat')return chat;if(action==='setup_status')return {first_run:false};if(action==='ui_layout_get')return DEFAULT_LAYOUT;
      if(action==='draft_get')return {revision:1,content:data.chat_id===chat.id ? {text:'Previously saved text',document_ids:[],skills:[],space_ids:[]} : {},scope_available:true};if(action==='draft_save' && data.chat_id===chat.id)return new Promise(resolve=>{finishSave=resolve;});if(action==='draft_confirm')return {ok:true,confirmed:true};
      if(action==='dictation_start')return new Promise(resolve=>{finishSpeech=resolve;});if(['projects','chats','runs','models','spaces','schedules','pending_questions'].includes(action))return {[action==='pending_questions'?'questions':action]:action==='chats'?[chat]:[]};return {};
    });window.pywebview={api:{call}};render(<App/>);await act(async()=>window.dispatchEvent(new Event('pywebviewready')));await screen.findByText('Saved speech fixture answer');await waitFor(()=>expect((screen.getByRole('textbox',{name:'Message Forge'}) as HTMLTextAreaElement).value).toBe('Previously saved text'));
    fireEvent.click(screen.getByRole('button',{name:'Start dictation'}));await waitFor(()=>expect(finishSpeech).toBeTruthy());await act(async()=>finishSpeech({state:'done',id:'saved-speech-job',text:'New speech',delivery_id:delivery}));await waitFor(()=>expect(finishSave).toBeTruthy());
    expect(call.mock.calls.some(([name])=>name==='draft_confirm')).toBe(false);const savedCall=call.mock.calls.find(([name,data])=>name==='draft_save' && data?.chat_id===chat.id)![1]!;expect(savedCall.content).toMatchObject({text:'Previously saved text New speech'});await act(async()=>finishSave({revision:2,content:savedCall.content,scope_available:true}));
    await waitFor(()=>expect(call).toHaveBeenCalledWith('draft_confirm',{scope_kind:'chat',chat_id:chat.id,project_id:null,expected_revision:2,delivery_ids:[delivery]}));expect(call.mock.calls.find(([name])=>name==='draft_confirm')![1]).not.toHaveProperty('attempt_id');
  });
  it('confirms a late native transcript in its original hidden scope without readiness content',async()=>{
    const chat={id:'dictation-fixture',title:'Dictation fixture',project_id:null,model:'fixture',messages:[{role:'assistant',content:'Dictation saved answer'}]};let finish!:(value:unknown)=>void;const delivery='e'.repeat(64);
    const call=vi.fn(async(action:string,data:Record<string,unknown>={})=>{
      if(action==='bootstrap')return {settings:{...DEFAULT_SETTINGS,model:'fixture'},projects:[],chats:[chat],runs:[],capabilities:{}};
      if(action==='get_chat')return chat;if(action==='setup_status')return {first_run:false};if(action==='ui_layout_get')return DEFAULT_LAYOUT;
      if(action==='draft_get')return {revision:0,content:{},scope_available:true};if(action==='draft_save')return {revision:1,content:data.content,scope_available:true};if(action==='draft_confirm')return {ok:true,receipt};
      if(action==='dictation_start')return new Promise(resolve=>{finish=resolve;});
      if(action==='update_handoff_begin')return {attempt_id:attempt,challenge:'b'.repeat(64)};if(action==='update_handoff_ack')return {phase:'prepared',confirmation:'c'.repeat(64)};if(action==='update_handoff_commit')return {attempt_id:attempt,phase:'committed'};
      if(action==='update_handoff_abort')return {aborted:true,not_executed:true};if(['projects','chats','runs','models','spaces','schedules','pending_questions'].includes(action))return {[action==='pending_questions'?'questions':action]:action==='chats'?[chat]:[]};return {};
    });window.pywebview={api:{call}};render(<App/>);await act(async()=>window.dispatchEvent(new Event('pywebviewready')));await screen.findByText('Dictation saved answer');fireEvent.click(screen.getByRole('button',{name:'Start dictation'}));await waitFor(()=>expect(finish).toBeTruthy());fireEvent.click(screen.getByRole('button',{name:/New chat/}));
    await act(async()=>finish({state:'done',id:'native-fixture-job',text:'Hidden transcript fixture',delivery_id:delivery}));expect((screen.getByRole('textbox',{name:'Message Forge'}) as HTMLTextAreaElement).value).toBe('');await act(async()=>{});await act(async()=>requestUpdateRestart());
    expect(call.mock.calls.find(([name])=>name==='dictation_start')?.[1]).toMatchObject({draft_scope:{scope_kind:'chat',chat_id:'dictation-fixture',project_id:null}});
    expect(call.mock.calls.find(([name,data])=>name==='draft_confirm' && data?.chat_id==='dictation-fixture')?.[1]).toMatchObject({delivery_ids:[delivery]});
    const ack=call.mock.calls.find(([name])=>name==='update_handoff_ack')?.[1];expect(ack).toBeTruthy();expect(JSON.stringify(ack)).not.toContain('Hidden transcript fixture');expect(JSON.stringify(ack)).not.toContain(delivery);
  });
  it('protects hidden images and blocks navigation while the native abort is unconfirmed',async()=>{
    const chat={id:'fixture-chat',title:'Fixture conversation',project_id:null,model:'fixture',messages:[{role:'assistant',content:'Saved fixture answer'}]};let cancel!:(value:unknown)=>void;
    const responses:Record<string,unknown>={bootstrap:{settings:{...DEFAULT_SETTINGS,model:'fixture'},projects:[],chats:[chat],runs:[],capabilities:{}},get_chat:chat,projects:{projects:[]},chats:{chats:[chat]},models:{models:[{name:'fixture'}]},runs:{runs:[]},schedules:{schedules:[]},spaces:{spaces:[]},pending_questions:{questions:[]},setup_status:{first_run:false},draft_get:{revision:0,content:{},scope_available:true},draft_save:({content}:Record<string,unknown>)=>({revision:1,content,scope_available:true}),draft_confirm:{ok:true,receipt},ui_layout_get:DEFAULT_LAYOUT,ui_layout_save:({layout}:Record<string,unknown>)=>layout,update_handoff_begin:{attempt_id:attempt,challenge:'b'.repeat(64)},update_handoff_abort:()=>new Promise(resolve=>{cancel=resolve;})};
    const call=vi.fn(async(action:string,data:Record<string,unknown>={})=>{const result=responses[action];return typeof result==='function'?result(data):result || {};});const screenshot=vi.fn(async()=>({image:'c3ludGhldGlj'}));window.pywebview={api:{call,screenshot}};
    render(<App/>);await act(async()=>window.dispatchEvent(new Event('pywebviewready')));await screen.findByText('Saved fixture answer');await act(async()=>fireEvent.click(screen.getByRole('button',{name:'Attach screenshot'})));await screen.findByRole('img',{name:'Attachment 1'});
    fireEvent.click(screen.getByRole('button',{name:/New chat/}));expect(screen.queryByRole('img',{name:'Attachment 1'})).toBeNull();await act(async()=>{});
    const restart=requestUpdateRestart();await waitFor(()=>expect(cancel).toBeTruthy());expect(document.querySelector<HTMLElement>('.app')!.inert).toBe(true);
    const revision=updateReadiness.getSnapshot().revision;fireEvent.keyDown(window,{key:'n',ctrlKey:true});expect(updateReadiness.getSnapshot().revision).toBe(revision);expect(screen.getByRole('status',{name:'Update restart'}).closest('.app')).toBeNull();
    await act(async()=>cancel({aborted:true,not_executed:true}));await restart;expect(document.querySelector<HTMLElement>('.app')!.inert).toBe(false);expect(updateReadiness.getSnapshot().reason).toBe('unsent_images');expect(call.mock.calls.some(([name])=>name==='update_handoff_ack' || name==='update_handoff_commit')).toBe(false);
    fireEvent.click(screen.getByRole('button',{name:'Fixture conversation'}));await screen.findByRole('img',{name:'Attachment 1'});fireEvent.click(screen.getByRole('button',{name:'Remove attachment 1'}));
    expect(call.mock.calls.filter(([name])=>name==='draft_save').every(([,data])=>!('images' in ((data as Record<string,unknown>).content as object)))).toBe(true);
  });
});
