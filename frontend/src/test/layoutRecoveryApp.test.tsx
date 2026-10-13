import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import App from '../App';
import { DEFAULT_SETTINGS } from '../types';
import { DEFAULT_LAYOUT } from '../workspaceLayout';
import { requestUpdateRestart } from '../updateHandoff';
import { updateReadiness } from '../updateReadiness';

const attempt='a'.repeat(32);
afterEach(()=>{updateReadiness.aborted(attempt);updateReadiness.dismiss();delete window.pywebview;});

async function workspace(failRetry:boolean){
  let loads=0;
  const call=vi.fn(async(action:string,data:Record<string,unknown>={})=>{
    if(action==='bootstrap')return {settings:{...DEFAULT_SETTINGS,model:'fixture'},projects:[],chats:[],runs:[],capabilities:{}};
    if(action==='ui_layout_get')return ++loads===1 || failRetry ? {error:'Synthetic layout temporarily unavailable'} : DEFAULT_LAYOUT;
    if(action==='ui_layout_save')return data.layout;
    if(action==='draft_get')return {revision:0,content:{},scope_available:true};
    if(action==='draft_save')return {revision:1,content:data.content,scope_available:true};
    if(action==='draft_confirm')return {ok:true,receipt:'d'.repeat(64)};
    if(action==='setup_status')return {first_run:false};
    if(action==='host_status')return {visible:true,visibility_seq:0};
    if(action==='update_handoff_begin')return {attempt_id:attempt,challenge:'b'.repeat(64)};
    if(action==='update_handoff_ack')return {phase:'prepared',confirmation:'c'.repeat(64)};
    if(action==='update_handoff_commit')return {attempt_id:attempt,phase:'committed'};
    if(action==='update_handoff_abort')return {attempt_id:attempt,aborted:true,not_executed:true};
    if(['projects','chats','runs','models','spaces','schedules','pending_questions'].includes(action))return {[action==='pending_questions'?'questions':action]:[]};
    return {};
  });
  window.pywebview={api:{call}};render(<App/>);
  await act(async()=>window.dispatchEvent(new Event('pywebviewready')));
  await waitFor(()=>expect(call).toHaveBeenCalledWith('ui_layout_get',{client_id:'desktop'}));
  await screen.findByText('Workspace layout could not be loaded. Retry before restarting.');
  await act(async()=>requestUpdateRestart());
  expect(call.mock.calls.some(([name])=>name==='update_handoff_ack')).toBe(false);
  return call;
}

describe('desktop workspace layout recovery',()=>{
  it('uses the visible recovery action and requires a fresh saved-work check',async()=>{
    const call=await workspace(false);
    fireEvent.click(screen.getByRole('button',{name:'Retry workspace layout'}));
    await waitFor(()=>expect(updateReadiness.getSnapshot().phase).toBe('idle'));
    expect(call.mock.calls.filter(([name])=>name==='ui_layout_get')).toHaveLength(2);
    expect(call.mock.calls.some(([name])=>name==='update_handoff_ack')).toBe(false);
    await act(async()=>requestUpdateRestart());
    expect(call.mock.calls.filter(([name])=>name==='update_handoff_ack')).toHaveLength(1);
    expect(call.mock.calls.filter(([name])=>name==='update_handoff_begin')).toHaveLength(2);
    expect(call.mock.calls.some(([name])=>name==='update_apply')).toBe(false);
  });
  it('retains the recovery action and sends no acknowledgement when the retry fails',async()=>{
    const call=await workspace(true);
    fireEvent.click(screen.getByRole('button',{name:'Retry workspace layout'}));
    await waitFor(()=>expect(call.mock.calls.filter(([name])=>name==='ui_layout_get')).toHaveLength(2));
    await waitFor(()=>expect((screen.getByRole('button',{name:'Retry workspace layout'}) as HTMLButtonElement).disabled).toBe(false));
    expect(updateReadiness.getSnapshot().phase).toBe('failed');
    expect(updateReadiness.getSnapshot().reason).toBe('preferences_pending');
    expect(call.mock.calls.some(([name])=>name==='update_handoff_ack' || name==='update_handoff_commit')).toBe(false);
  });
});
