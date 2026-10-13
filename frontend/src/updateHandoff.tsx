import { api, isNative } from './api';
import { useLayoutEffect, useRef } from 'react';
import { readinessMessages, updateReadiness, useUpdateReadiness, type ReadinessReason } from './updateReadiness';
import './updateHandoff.css';

let starting = false;
let retryAbort: (()=>Promise<void>) | undefined;
let stopPreviousSignals: (()=>void) | undefined;
interface NativeHandoffResult {attempt_id?:string;phase?:string;confirmation?:string;blocked?:boolean;reason?:unknown;aborted?:boolean;not_executed?:boolean}
const allowedReason=(value:unknown):ReadinessReason|undefined=>typeof value==='string' && Object.prototype.hasOwnProperty.call(readinessMessages,value) ? value as ReadinessReason : undefined;
/** Challenges and confirmations stay in this closure, never UI state or logs. */
export async function requestUpdateRestart(kind:'apply'|'rollback'='apply') {
  if(starting || !isNative() || !updateReadiness.canEdit())return;
  stopPreviousSignals?.();stopPreviousSignals=undefined;
  starting=true;
  let attemptId='';
  let reason:ReadinessReason|undefined;
  let cancellation:Promise<void>|undefined;
  let cancellationAcknowledged=false,ownsAttempt=false;
  let stopAbortSignals=()=>{};
  const acknowledgeAbort=(result:NativeHandoffResult)=>{
    if(result.attempt_id!==undefined && result.attempt_id!==attemptId)return false;
    reason ||= allowedReason(result.reason);
    if(result.aborted!==true || result.not_executed!==true)return false;
    cancellationAcknowledged=true;stopAbortSignals();if(ownsAttempt)updateReadiness.aborted(attemptId,reason);retryAbort=undefined;return true;
  };
  const abort=()=> {
    if(cancellationAcknowledged)return Promise.resolve();
    if(cancellation)return cancellation;
    if(!attemptId)return Promise.resolve();
    if(ownsAttempt)updateReadiness.aborting(reason || 'workspace_changed');
    cancellation=(async()=>{
      try {
        const result=await api<NativeHandoffResult>('update_handoff_abort',{attempt_id:attemptId});
        acknowledgeAbort(result);
      } catch { /* Keep input locked until native cancellation is confirmed. */ }
      finally { cancellation=undefined; }
    })();
    return cancellation;
  };
  try {
    const challenge=await api<{attempt_id:string;challenge:string;blocked?:boolean;reason?:unknown}>('update_handoff_begin',{kind});
    if(challenge.blocked){updateReadiness.blocked(allowedReason(challenge.reason) || 'protocol_error');return;}
    if(!/^[a-f0-9]{32}$/.test(challenge.attempt_id)){updateReadiness.blocked('workspace_changed');return;}
    attemptId=challenge.attempt_id;retryAbort=abort;
    let statusPending=false;
    const changed=(event:Event)=>{
      if((event as CustomEvent).detail?.attempt_id!==attemptId || !updateReadiness.isLocked() || statusPending)return;
      statusPending=true;void api<NativeHandoffResult>('update_handoff_status',{attempt_id:attemptId}).then(result=>{
        if(result.attempt_id!==undefined && result.attempt_id!==attemptId)return;
        if(acknowledgeAbort(result))return;
        if(result.blocked || result.phase==='failed'){reason ||= allowedReason(result.reason) || 'protocol_error';void abort();}
      }).catch(()=>{}).finally(()=>{statusPending=false;});
    };
    window.addEventListener('forge:update-handoff',changed);stopAbortSignals=()=>window.removeEventListener('forge:update-handoff',changed);stopPreviousSignals=stopAbortSignals;
    ownsAttempt=true;
    if(!updateReadiness.start(attemptId,()=>{void abort();})){ownsAttempt=false;await abort();return;}
    if(typeof challenge.challenge!=='string' || challenge.challenge.length<16 || challenge.challenge.length>512){reason='workspace_changed';await abort();return;}
    const confirmed=await updateReadiness.confirm(attemptId);
    if(!confirmed.ok){reason=confirmed.reason;await abort();return;}
    const revision=confirmed.revision!, participantCount=confirmed.participantCount!;
    if(!updateReadiness.unchanged(attemptId,revision)){await abort();return;}
    const prepared=await api<NativeHandoffResult>('update_handoff_ack',{attempt_id:attemptId,challenge:challenge.challenge,workspace_revision:revision,participant_count:participantCount,receipts:confirmed.receipts});
    if(prepared.blocked || prepared.phase!=='prepared' || typeof prepared.confirmation!=='string' || prepared.confirmation.length<16 || prepared.confirmation.length>512 || !updateReadiness.prepared(attemptId,revision)){reason ||= allowedReason(prepared.reason) || 'protocol_error';await abort();return;}
    if(!updateReadiness.unchanged(attemptId,revision)){await abort();return;}
    const committed=await api<NativeHandoffResult>('update_handoff_commit',{attempt_id:attemptId,confirmation:prepared.confirmation,workspace_revision:revision,participant_count:participantCount});
    if(committed.blocked || committed.phase!=='committed' || committed.attempt_id!==attemptId){reason ||= allowedReason(committed.reason) || 'protocol_error';await abort();return;}
    // Only the native owner may quit, after authenticated helper READY.
  } catch { if(attemptId){reason=reason || 'workspace_changed';await abort();}else updateReadiness.blocked('workspace_changed'); }
  finally {starting=false;}
}

export function UpdateHandoffSurface() {
  const snapshot=useUpdateReadiness();
  const surface=useRef<HTMLElement>(null),previousFocus=useRef<HTMLElement|null>(null);
  useLayoutEffect(()=>{if(updateReadiness.isLocked()){if(!previousFocus.current)previousFocus.current=document.activeElement as HTMLElement;surface.current?.querySelector<HTMLButtonElement>('button')?.focus();}else if(previousFocus.current){if(previousFocus.current.isConnected)previousFocus.current.focus();previousFocus.current=null;}},[snapshot.phase]);
  if(snapshot.phase==='idle')return null;
  const locked=updateReadiness.isLocked();
  const message=snapshot.reason ? readinessMessages[snapshot.reason] : snapshot.phase==='checking' ? 'Saving your work before restarting…' : snapshot.phase==='prepared' ? 'Preparing the verified restart. Forge will stay open until it is ready.' : 'Confirming your saved work…';
  return <section ref={surface} className="update-handoff-surface" aria-label="Update restart" role="status" aria-live="polite">
    <strong>{locked ? 'Restart check' : 'Restart did not start. Forge is still open.'}</strong><p>{message}</p>
    {locked && <button type="button" className="secondary" onClick={()=>{if(snapshot.phase==='aborting')void retryAbort?.();else updateReadiness.requestAbort();}}>{snapshot.phase==='aborting'?'Confirm cancellation':'Keep working'}</button>}
    {!locked && snapshot.reason==='native_input' && isNative() && <button type="button" className="secondary" onClick={()=>void api<{closed?:boolean}>('update_handoff_close_children').then(result=>{if(result.closed)updateReadiness.dismiss();}).catch(()=>{})}>Close browser work…</button>}
    {!locked && snapshot.reason==='paired_editor' && isNative() && <button type="button" className="secondary" onClick={()=>void api<{disconnected?:boolean}>('update_handoff_disconnect_editors').then(result=>{if(result.disconnected)updateReadiness.dismiss();}).catch(()=>{})}>Disconnect paired browsers…</button>}
    {!locked && <button type="button" className="text-button" onClick={()=>updateReadiness.dismiss()}>Keep working</button>}
  </section>;
}
