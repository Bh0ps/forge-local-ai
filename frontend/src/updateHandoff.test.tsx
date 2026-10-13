import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen } from '@testing-library/react';
const {call}=vi.hoisted(()=>({call:vi.fn()}));
vi.mock('./api',()=>({api:call,isNative:()=>true}));
import { requestUpdateRestart, UpdateHandoffSurface } from './updateHandoff';
import { installReadinessFence, updateReadiness } from './updateReadiness';
const attempt='a'.repeat(32),challenge='b'.repeat(64),confirmation='c'.repeat(64),receipt='d'.repeat(64);
const deferred=<T,>()=>{let resolve!:(value:T)=>void;const promise=new Promise<T>(done=>{resolve=done;});return {promise,resolve};};
let handles:ReturnType<typeof updateReadiness.register>[]=[];
beforeEach(()=>{call.mockReset();call.mockImplementation(async(action:string)=>action==='update_handoff_begin'?{attempt_id:attempt,challenge}:action==='update_handoff_ack'?{phase:'prepared',confirmation}:action==='update_handoff_commit'?{attempt_id:attempt,phase:'committed'}:action==='update_handoff_abort'?{aborted:true,not_executed:true}:{});});
afterEach(()=>{updateReadiness.aborted(attempt);handles.forEach(handle=>handle.detach());handles=[];updateReadiness.dismiss();});
describe('explicit native restart handshake',()=>{
  it('requires confirmed layout recovery and restores the restart trigger focus',async()=>{
    const save=deferred<boolean>();let blocked=true;
    handles.push(updateReadiness.register({inspect:()=>({reasons:blocked?['preferences_pending']:[]}),flushAndConfirm:async()=>({ok:true,receipts:[]})}));
    const retry=vi.fn(()=>save.promise);
    render(<><button>Restart fixture</button><UpdateHandoffSurface layoutRecovery={{error:'load',retrying:false,retry}}/></>);
    const trigger=screen.getByRole('button',{name:'Restart fixture'});trigger.focus();
    await act(async()=>requestUpdateRestart());
    const action=screen.getByRole('button',{name:'Retry workspace layout'});action.focus();
    fireEvent.click(action);expect(retry).toHaveBeenCalledOnce();expect(updateReadiness.getSnapshot().reason).toBe('preferences_pending');
    blocked=false;await act(async()=>save.resolve(true));
    expect(updateReadiness.getSnapshot().phase).toBe('idle');expect(document.activeElement).toBe(trigger);
    expect(call.mock.calls.some(([name])=>name==='update_handoff_ack')).toBe(false);
  });
  it('keeps failed layout retries blocked and does not dismiss a newer failure',async()=>{
    updateReadiness.blocked('preferences_pending');const late=deferred<boolean>(),retry=vi.fn().mockResolvedValueOnce(false).mockImplementationOnce(()=>late.promise);
    render(<UpdateHandoffSurface layoutRecovery={{error:'save',retrying:false,retry}}/>);
    await act(async()=>fireEvent.click(screen.getByRole('button',{name:'Retry workspace layout'})));
    expect(updateReadiness.getSnapshot().reason).toBe('preferences_pending');
    fireEvent.click(screen.getByRole('button',{name:'Retry workspace layout'}));
    act(()=>{updateReadiness.dismiss();updateReadiness.blocked('preferences_pending');});
    const newer=updateReadiness.getSnapshot();await act(async()=>late.resolve(true));expect(updateReadiness.getSnapshot()).toBe(newer);
  });
  it('shows a disabled, announced retry while layout recovery is pending',()=>{
    updateReadiness.blocked('preferences_pending');render(<UpdateHandoffSurface layoutRecovery={{error:null,retrying:true,retry:vi.fn()}}/>);
    const retry=screen.getByRole('button',{name:'Retrying workspace layout…'});expect(retry.getAttribute('aria-busy')).toBe('true');expect((retry as HTMLButtonElement).disabled).toBe(true);
  });
  it('routes an explicit rollback through the same opaque saved-work acknowledgment and fence',async()=>{
    handles.push(updateReadiness.register({inspect:()=>({reasons:[]}),flushAndConfirm:async()=>({ok:true,receipts:[receipt]})}));await requestUpdateRestart('rollback');
    expect(call).toHaveBeenCalledWith('update_handoff_begin',{kind:'rollback'});const ack=call.mock.calls.find(([name])=>name==='update_handoff_ack')![1];const commit=call.mock.calls.find(([name])=>name==='update_handoff_commit')![1];expect(ack.receipts).toEqual([receipt]);expect(commit).toMatchObject({attempt_id:attempt,workspace_revision:ack.workspace_revision,participant_count:ack.participant_count});expect(call.mock.calls.some(([name])=>name==='update_rollback')).toBe(false);expect(updateReadiness.canEdit()).toBe(false);
  });
  it('aborts a resolved blocked commit and preserves the allowlisted helper failure',async()=>{
    call.mockImplementation(async(action:string)=>action==='update_handoff_begin'?{attempt_id:attempt,challenge}:action==='update_handoff_ack'?{phase:'prepared',confirmation}:action==='update_handoff_commit'?{attempt_id:attempt,phase:'failed',blocked:true,reason:'helper_failed'}:action==='update_handoff_abort'?{attempt_id:attempt,aborted:true,not_executed:true}:{});
    await requestUpdateRestart();expect(call).toHaveBeenCalledWith('update_handoff_abort',{attempt_id:attempt});expect(updateReadiness.canEdit()).toBe(true);expect(updateReadiness.getSnapshot().reason).toBe('helper_failed');render(<UpdateHandoffSurface/>);expect(screen.getByText('The update helper could not start safely. Keep Forge open and retry.')).toBeTruthy();
  });
  it('retains a typed native input refusal after a successful abort of acknowledgment',async()=>{
    call.mockImplementation(async(action:string)=>action==='update_handoff_begin'?{attempt_id:attempt,challenge}:action==='update_handoff_ack'?{attempt_id:attempt,blocked:true,phase:'failed',reason:'native_input'}:action==='update_handoff_abort'?{attempt_id:attempt,aborted:true,not_executed:true}:{});
    await requestUpdateRestart();expect(updateReadiness.getSnapshot().reason).toBe('native_input');expect(updateReadiness.canEdit()).toBe(true);expect(call.mock.calls.some(([name])=>name==='update_handoff_commit')).toBe(false);
  });
  it.each(['checking','prepared'] as const)('verifies native cancellation during %s and ignores another attempt event',async phase=>{
    const waiting=deferred<{ok:true;receipts:string[]}>(),committing=deferred<unknown>();let confirmed=false;
    handles.push(updateReadiness.register({inspect:()=>({reasons:[]}),flushAndConfirm:()=>phase==='checking'?waiting.promise:Promise.resolve({ok:true,receipts:[]})}));
    call.mockImplementation((action:string)=>Promise.resolve(action==='update_handoff_begin'?{attempt_id:attempt,challenge}:action==='update_handoff_ack'?{phase:'prepared',confirmation}:action==='update_handoff_status'?{attempt_id:attempt,phase:confirmed?'failed':phase,reason:'native_input',aborted:confirmed,not_executed:confirmed}:action==='update_handoff_abort'?{attempt_id:attempt,aborted:true,not_executed:true}:action==='update_handoff_commit'?committing.promise:{}));
    const operation=requestUpdateRestart();await vi.waitFor(()=>expect(updateReadiness.getSnapshot().phase).toBe(phase));
    await act(async()=>window.dispatchEvent(new CustomEvent('forge:update-handoff',{detail:{attempt_id:'e'.repeat(32),aborted:true,not_executed:true}})));expect(call.mock.calls.some(([name])=>name==='update_handoff_status')).toBe(false);
    await act(async()=>window.dispatchEvent(new CustomEvent('forge:update-handoff',{detail:{attempt_id:attempt,aborted:true,not_executed:true}})));expect(updateReadiness.canEdit()).toBe(false);
    confirmed=true;await act(async()=>window.dispatchEvent(new CustomEvent('forge:update-handoff',{detail:{attempt_id:attempt}})));expect(updateReadiness.canEdit()).toBe(true);expect(updateReadiness.getSnapshot().reason).toBe('native_input');
    waiting.resolve({ok:true,receipts:[]});committing.resolve({attempt_id:attempt,phase:'failed',blocked:true,reason:'native_input'});await operation;expect(updateReadiness.canEdit()).toBe(true);
  });
  it('offers explicit paired browser disconnection without any readiness metadata and preserves cancellation',async()=>{
    call.mockImplementation(async(action:string)=>action==='update_handoff_begin'?{blocked:true,reason:'paired_editor'}:action==='update_handoff_disconnect_editors'?{disconnected:false,cancelled:true}:{});await requestUpdateRestart();render(<UpdateHandoffSurface/>);
    expect(screen.getByText('Save your work in paired browsers, then disconnect them before restarting.')).toBeTruthy();await act(async()=>fireEvent.click(screen.getByRole('button',{name:'Disconnect paired browsers…'})));expect(call).toHaveBeenCalledWith('update_handoff_disconnect_editors');expect(updateReadiness.getSnapshot().reason).toBe('paired_editor');
    call.mockResolvedValueOnce({disconnected:true,cancelled:false});await act(async()=>fireEvent.click(screen.getByRole('button',{name:'Disconnect paired browsers…'})));expect(updateReadiness.getSnapshot().phase).toBe('idle');expect(call.mock.calls.some(([name])=>name==='update_handoff_ack')).toBe(false);
  });
  it('treats a delayed native event as a wake hint and verifies cancellation over the bridge',async()=>{
    let confirmed=false;call.mockImplementation(async(action:string)=>action==='update_handoff_begin'?{attempt_id:attempt,challenge}:action==='update_handoff_abort'?{aborted:false,locked:true}:action==='update_handoff_status'?{aborted:confirmed,not_executed:confirmed}:{});
    handles.push(updateReadiness.register({inspect:()=>({reasons:['dialog_open']}),flushAndConfirm:async()=>({ok:true,receipts:[]})}));await requestUpdateRestart();expect(updateReadiness.canEdit()).toBe(false);
    await act(async()=>window.dispatchEvent(new CustomEvent('forge:update-handoff',{detail:{attempt_id:attempt,aborted:true,not_executed:true}})));expect(updateReadiness.canEdit()).toBe(false);
    confirmed=true;await act(async()=>window.dispatchEvent(new CustomEvent('forge:update-handoff',{detail:{attempt_id:attempt}})));expect(call).toHaveBeenCalledWith('update_handoff_status',{attempt_id:attempt});expect(updateReadiness.canEdit()).toBe(true);
  });
  it('aborts a valid native reservation when its challenge is malformed',async()=>{
    call.mockImplementation(async(action:string)=>action==='update_handoff_begin'?{attempt_id:attempt,challenge:'invalid'}:{aborted:true,not_executed:true});await requestUpdateRestart();
    expect(call.mock.calls.map(([name])=>name)).toEqual(['update_handoff_begin','update_handoff_abort']);expect(call).toHaveBeenCalledWith('update_handoff_abort',{attempt_id:attempt});expect(updateReadiness.canEdit()).toBe(true);
  });
  it('sends only bounded opaque confirmations and commits the same locked snapshot',async()=>{
    handles.push(updateReadiness.register({inspect:()=>({reasons:[]}),flushAndConfirm:async()=>({ok:true,receipts:[receipt]})}));
    const clear=installReadinessFence();await requestUpdateRestart();
    const ack=call.mock.calls.find(([name])=>name==='update_handoff_ack')![1];const commit=call.mock.calls.find(([name])=>name==='update_handoff_commit')![1];
    expect(Object.keys(ack).sort()).toEqual(['attempt_id','challenge','participant_count','receipts','workspace_revision']);expect(commit.workspace_revision).toBe(ack.workspace_revision);expect(commit.participant_count).toBe(ack.participant_count);
    expect(window.__forgeUpdateReadiness!.check(ack).ok).toBe(true);expect(updateReadiness.canEdit()).toBe(false);clear();
  });
  it('blocks an unsent image without acknowledging or invoking an installer',async()=>{
    handles.push(updateReadiness.register({inspect:()=>({reasons:['unsent_images']}),flushAndConfirm:async()=>({ok:true,receipts:[]})}));await requestUpdateRestart();
    expect(call.mock.calls.map(([name])=>name)).toEqual(['update_handoff_begin','update_handoff_abort']);expect(updateReadiness.getSnapshot().reason).toBe('unsent_images');expect(updateReadiness.canEdit()).toBe(true);
  });
  it('holds the lock until abort is acknowledged and rejects a late preparation response',async()=>{
    const prepared=deferred<unknown>(),cancelled=deferred<unknown>();call.mockImplementation((action:string)=>action==='update_handoff_begin'?Promise.resolve({attempt_id:attempt,challenge}):action==='update_handoff_ack'?prepared.promise:action==='update_handoff_abort'?cancelled.promise:Promise.resolve({}));
    const handle=updateReadiness.register({inspect:()=>({reasons:[]}),flushAndConfirm:async()=>({ok:true,receipts:[]})});handles.push(handle);
    const operation=requestUpdateRestart();await vi.waitFor(()=>expect(call).toHaveBeenCalledWith('update_handoff_ack',expect.any(Object)));handle.changed();
    expect(updateReadiness.canEdit()).toBe(false);cancelled.resolve({aborted:true,not_executed:true});await vi.waitFor(()=>expect(updateReadiness.canEdit()).toBe(true));prepared.resolve({phase:'prepared',confirmation});await operation;
    expect(call.mock.calls.some(([name])=>name==='update_handoff_commit')).toBe(false);expect(updateReadiness.canEdit()).toBe(true);expect(call.mock.calls.filter(([name])=>name==='update_handoff_abort')).toHaveLength(1);
  });
  it('never unlocks after a lost cancellation reply, and supports a confirmed retry',async()=>{
    let aborts=0;call.mockImplementation(async(action:string)=>action==='update_handoff_begin'?{attempt_id:attempt,challenge}:action==='update_handoff_abort'?(++aborts===1?Promise.reject(new Error('synthetic-secret')):{aborted:true,not_executed:true}):{});
    handles.push(updateReadiness.register({inspect:()=>({reasons:['dialog_open']}),flushAndConfirm:async()=>({ok:true,receipts:[]})}));await requestUpdateRestart();expect(updateReadiness.canEdit()).toBe(false);
    render(<UpdateHandoffSurface/>);expect(screen.queryByText('synthetic-secret')).toBeNull();await act(async()=>fireEvent.click(screen.getByRole('button',{name:'Confirm cancellation'})));expect(updateReadiness.canEdit()).toBe(true);
  });
  it('offers explicit native child closure only after a typed blocker',async()=>{
    call.mockResolvedValue({blocked:true,reason:'native_input'});await requestUpdateRestart();render(<UpdateHandoffSurface/>);expect(call).toHaveBeenCalledOnce();expect(screen.getByRole('button',{name:'Close browser work…'})).toBeTruthy();expect(updateReadiness.canEdit()).toBe(true);
  });
});
