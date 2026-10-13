import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { api, errorText } from './api';
import { updateReadiness, useReadinessParticipant, type ReadinessReason, type SaveConfirmation, type RetainedRegistration } from './updateReadiness';

export interface DraftScope { scope_kind: 'chat' | 'new_chat' | 'builder'; project_id?: string | null; chat_id?: string; builder_id?: string; }
export interface DraftContent { text?: string; document_ids?: string[]; skills?: string[]; space_ids?: string[]; brief?: Record<string, unknown>; base_revision?: number; }
interface DraftRecord { revision: number; content: DraftContent; stale_refs?: unknown[]; scope_available?: boolean; }
const encoded = (value: DraftContent) => JSON.stringify(value);
interface ScopeState {revision:number;saved:string;ready:boolean;blocked:boolean;scope:DraftScope;failure?:ReadinessReason;stale?:boolean;unavailable?:boolean;deliveries?:Set<string>}
interface LocalSnapshot {key:string;content:DraftContent;fingerprint:string}
interface PrivateSession {latest:{current:Map<string,LocalSnapshot>};records:{current:Map<string,ScopeState>};queue:{current:Promise<void>};pending:{current:number};deletedChats:Set<string>;registration:RetainedRegistration}
// A Builder view can unmount while its controller still owns unsaved work.
// Only that owner's callback registration is shared with readiness.
const retainedSessions=new Map<string,PrivateSession>();
const nativeSessions=new WeakMap<object,Map<string,PrivateSession>>();
function privateSession(owner?:string):PrivateSession {
  const bridge=window.pywebview?.api;
  let sessions=retainedSessions;
  if(bridge){sessions=nativeSessions.get(bridge) || new Map<string,PrivateSession>();nativeSessions.set(bridge,sessions);}
  const previous=owner && sessions.get(owner);if(previous)return previous;
  const session:PrivateSession={
    latest:{current:new Map()},records:{current:new Map()},queue:{current:Promise.resolve()},pending:{current:0},deletedChats:new Set(),
    registration:{handle:{current:null},latest:{current:{inspect:()=>({reasons:[]}),flushAndConfirm:async()=>({ok:true,receipts:[]})}}},
  };
  if(owner)sessions.set(owner,session);return session;
}

/** CAS drafts are local to Forge. Images deliberately never enter this store. */
export function usePersistentDraft(scope: DraftScope, content: DraftContent, restore: (content: DraftContent) => void, enabled = true, owner?:string) {
  const key = JSON.stringify(scope), fingerprint = encoded(content);
  const session=useRef<PrivateSession|undefined>(undefined);session.current ||= privateSession(owner);
  const latest=session.current.latest,records=session.current.records,queue=session.current.queue,pending=session.current.pending;
  const deleted=(target:DraftScope)=>target.scope_kind==='chat' && session.current!.deletedChats.has(target.chat_id || '');
  const visited=useRef(new Set<string>()),localRestore=useRef<LocalSnapshot|undefined>(undefined);
  const current = useRef({ key, content, fingerprint }); current.current = { key, content, fingerprint };
  if(enabled && !visited.current.has(key)){visited.current.add(key);if(owner && latest.current.has(key))localRestore.current=latest.current.get(key);}
  if(!deleted(scope) && (!localRestore.current || localRestore.current.key!==key))latest.current.set(key,current.current);
  const mounted = useRef(true);
  const [status, setStatus] = useState(''), [conflict, setConflict] = useState(false);
  const restoreRef = useRef(restore); restoreRef.current = restore;
  function enqueue(operation: () => Promise<void>) { pending.current++; queue.current = queue.current.catch(() => {}).then(operation).finally(() => { pending.current--; readiness.settled(); }); return queue.current; }
  const reason = () => [...records.current.values()].map(record => record.failure || (!record.ready ? 'draft_restoring' : record.blocked ? 'draft_conflict' : record.unavailable ? 'draft_scope_unavailable' : record.stale ? 'draft_references' : undefined)).find(Boolean) as ReadinessReason | undefined;
  const readiness = useReadinessParticipant({
    inspect: () => ({ reasons: reason() ? [reason()!] : [] }),
    retain: () => pending.current > 0 || Boolean(reason()) || [...records.current.entries()].some(([scopeKey,record])=>record.deliveries?.size || latest.current.get(scopeKey)?.fingerprint!==record.saved),
    flushAndConfirm: async ({attemptId, signal}): Promise<SaveConfirmation> => {
      await queue.current;
      const failure = reason(); if (failure) return {ok:false, reason:failure};
      const receipts: string[] = [];
      for (const [scopeKey, record] of records.current) {
        const captured = latest.current.get(scopeKey); if (!captured) return {ok:false, reason:'draft_save_failed'};
        await save(captured); await queue.current;
        if (signal.aborted || latest.current.get(scopeKey)?.fingerprint !== captured.fingerprint) return {ok:false, reason:'workspace_changed'};
        const failed = reason(); if (failed) return {ok:false, reason:failed};
        if (record.saved !== captured.fingerprint) return {ok:false, reason:'draft_save_failed'};
        try {
          const deliveryIds=[...(record.deliveries || [])],groups=deliveryIds.length ? Array.from({length:Math.ceil(deliveryIds.length/16)},(_,index)=>deliveryIds.slice(index*16,index*16+16)) : [[]];
          for(const group of groups){
            if(signal.aborted)return {ok:false,reason:'workspace_changed'};
            const result = await api<{receipt:string}>('draft_confirm', {...record.scope, expected_revision:record.revision, attempt_id:attemptId,...(group.length ? {delivery_ids:group} : {})});
            if (typeof result.receipt !== 'string' || !/^[a-f0-9]{64}$/.test(result.receipt)) return {ok:false, reason:'draft_save_failed'};
            receipts.push(result.receipt);group.forEach(id=>record.deliveries?.delete(id));
          }
        } catch { return {ok:false, reason:'draft_save_failed'}; }
      }
      return {ok:true, receipts};
    },
  },session.current.registration);
  useLayoutEffect(()=>{const retained=localRestore.current;if(enabled && retained?.key===key){localRestore.current=undefined;current.current=retained;restoreRef.current(retained.content);}},[key,enabled]);
  const observed=useRef({key,fingerprint});
  useLayoutEffect(()=>{if(observed.current.key!==key || observed.current.fingerprint!==fingerprint){observed.current={key,fingerprint};readiness.changed();}},[key,fingerprint]);
  useEffect(() => { mounted.current=true; return () => { mounted.current=false; }; }, []);
  async function confirmSavedDeliveries(record:ScopeState,captured:LocalSnapshot) {
    if(deleted(record.scope) || !record.ready || record.blocked || record.revision<1 || record.saved!==captured.fingerprint || latest.current.get(captured.key)?.fingerprint!==captured.fingerprint)return;
    const revision=record.revision,ids=[...(record.deliveries || [])];
    for(let index=0;index<ids.length;index+=16){
      if(deleted(record.scope) || record.revision!==revision || record.saved!==captured.fingerprint || latest.current.get(captured.key)?.fingerprint!==captured.fingerprint)return;
      const group=ids.slice(index,index+16);
      try {
        // This confirms saved speech only. It cannot grant a restart lease.
        const result=await api<{ok?:boolean;confirmed?:boolean}>('draft_confirm',{...record.scope,expected_revision:revision,delivery_ids:group});
        if(result.ok!==true || result.confirmed!==true || deleted(record.scope))return;
        group.forEach(id=>record.deliveries?.delete(id));
      } catch { return; } // Keep IDs for an exact retry or handoff confirmation.
    }
  }
  async function save(captured = current.current) {
    const record = records.current.get(captured.key);
    if (!record?.ready || deleted(record.scope) || record.blocked) return;
    await enqueue(async () => {
      if (deleted(record.scope) || record.blocked) return;
      // A queued old revision cannot rewrite a newer edit or a cleared draft.
      if (latest.current.get(captured.key)?.fingerprint!==captured.fingerprint) return;
      if(captured.fingerprint===record.saved){await confirmSavedDeliveries(record,captured);return;}
      try {
        const result = await api<DraftRecord>('draft_save', { ...record.scope, expected_revision: record.revision, content: captured.content });
        if(deleted(record.scope))return;
        record.revision = result.revision; record.saved = captured.fingerprint; record.stale=Boolean(result.stale_refs?.length); record.unavailable=result.scope_available===false;
        if (current.current.key === captured.key) { setStatus(result.stale_refs?.length ? 'Draft saved locally; some references are unavailable' : 'Draft saved locally'); setConflict(false); }
        await confirmSavedDeliveries(record,captured);
      } catch (error) {
        if(deleted(record.scope))return;
        record.blocked = true; record.failure=errorText(error).includes('another window') ? 'draft_conflict' : 'draft_save_failed';
        if (current.current.key === captured.key) { setStatus(`Draft kept in this window: ${errorText(error)}`); setConflict(true); }
      }
    });
  }
  useEffect(() => {
    if (!enabled || deleted(scope)) return;
    setStatus(''); setConflict(false);
    if (records.current.has(key)){const existing=records.current.get(key)!;if(existing.failure || existing.blocked){setConflict(true);setStatus('Draft kept in this window. Reload the saved draft to reconcile.');}return () => { const captured = latest.current.get(key); if (captured) void save(captured); };}
    const initial = current.current.fingerprint;
    const initialContent=current.current.content;
    const hadLocalWork=scope.scope_kind!=='builder' && Boolean(initialContent.text || initialContent.document_ids?.length || initialContent.skills?.length || initialContent.space_ids?.length);
    const record: {revision:number;saved:string;ready:boolean;blocked:boolean;scope:DraftScope;failure?:ReadinessReason;stale?:boolean;unavailable?:boolean} = { revision: 0, saved: initial, ready: false, blocked: false, scope }; records.current.set(key, record);
    void api<DraftRecord>('draft_get', {...scope}).then(result => {
      if(deleted(scope))return;
      record.revision = result.revision || 0; record.ready = true; record.stale=Boolean(result.stale_refs?.length); record.unavailable=result.scope_available===false;
      const restored = result.content || {};
      // Typing and attachment changes during restoration always win locally.
      if (!hadLocalWork && current.current.key === key && current.current.fingerprint === initial && Object.keys(restored).length) {
        record.saved = encoded(restored); restoreRef.current(restored);
        setStatus(result.stale_refs?.length ? 'Draft restored; some references are unavailable' : 'Draft restored locally');
      } else { record.saved = encoded(restored); void save(latest.current.get(key) || current.current); }
    }).catch(error => {if(deleted(scope))return; record.failure='draft_save_failed'; if (current.current.key === key && mounted.current){setStatus(`Draft restoration unavailable: ${errorText(error)}`);setConflict(true);} }).finally(()=>readiness.settled());
    // The prior key is flushed before navigation/unmount; captured content avoids
    // saving a new chat's draft into the previous chat.
    return () => { const captured = latest.current.get(key); if (captured) void save(captured); };
  }, [key, enabled]);
  useEffect(() => { if (!enabled) return; const captured = { key, content, fingerprint }; const timer = setTimeout(() => void save(captured), 500); return () => clearTimeout(timer); }, [key, fingerprint, enabled]);
  async function discard() {
    if (!updateReadiness.canEdit()) return; readiness.changed();
    const captured = current.current, record = records.current.get(captured.key); if (!record || deleted(record.scope)) return;
    await enqueue(async () => {
      if(deleted(record.scope))return;
      try {
        // Discarding this window's conflicting edits preserves the other
        // window's saved version. Never rebase a delete onto its newer revision.
        if (record.blocked) {
          const latest = await api<DraftRecord>('draft_get', {...record.scope});
          if(deleted(record.scope))return;
          record.revision = latest.revision; record.saved = encoded(latest.content || {}); record.blocked = false; record.failure=undefined; record.ready=true; record.stale=Boolean(latest.stale_refs?.length); record.unavailable=latest.scope_available===false;
          if (current.current.key === captured.key) {restoreRef.current(latest.content || {});setConflict(false);setStatus('Local edits discarded; latest saved draft restored');}
          return;
        }
        const result = await api<DraftRecord>('draft_clear', { ...record.scope, expected_revision: record.revision });
        if(deleted(record.scope))return;
        record.revision = result.revision; record.saved = encoded({}); record.blocked = false; record.failure=undefined; record.stale=false; record.unavailable=false;
        if(latest.current.get(captured.key)?.fingerprint===captured.fingerprint){latest.current.set(captured.key,{...captured,content:{},fingerprint:encoded({})});if(current.current.key===captured.key){current.current={...captured,content:{},fingerprint:encoded({})};restoreRef.current({});setConflict(false);setStatus('Draft discarded');}}
      } catch (error) { if(!deleted(record.scope)){setStatus(errorText(error)); setConflict(true);} }
    });
  }
  async function reloadSaved() {
    if (!updateReadiness.canEdit()) return; readiness.changed();
    const captured=current.current, record=records.current.get(captured.key); if(!record || deleted(record.scope))return;
    await enqueue(async()=>{if(deleted(record.scope))return;try{const result=await api<DraftRecord>('draft_get',{...record.scope});if(deleted(record.scope))return;record.revision=result.revision;record.saved=encoded(result.content||{});record.ready=true;record.blocked=false;record.failure=undefined;record.stale=Boolean(result.stale_refs?.length);record.unavailable=result.scope_available===false;if(current.current.key===captured.key){restoreRef.current(result.content||{});setConflict(false);setStatus('Latest saved draft restored');}}catch(error){if(deleted(record.scope))return;record.failure='draft_save_failed';if(current.current.key===captured.key)setStatus(errorText(error));}});
  }
  async function clearSubmitted(scopeKey: string, submitted: string) {
    await enqueue(async () => {
      const record = records.current.get(scopeKey);
      if (!record?.ready || deleted(record.scope) || record.blocked || record.saved !== submitted) return;
      const local = latest.current.get(scopeKey);
      if (local && local.fingerprint !== submitted && local.fingerprint !== encoded({})) return;
      try {
        const result = await api<DraftRecord>('draft_clear', {...record.scope, expected_revision: record.revision});
        if(deleted(record.scope))return;
        record.revision = result.revision; record.saved = encoded({});
      } catch (error) { if(deleted(record.scope))return;record.blocked = true; record.failure='draft_conflict'; if (current.current.key === scopeKey) {setConflict(true); setStatus(`Submitted; saved draft needs reconciliation: ${errorText(error)}`);} }
    });
  }
  // Input producers carry their original scope. A hidden draft must receive its
  // late document/transcript result before the active view changes again.
  function captureScope(target: DraftScope, value: DraftContent) {
    if(deleted(target))return;
    readiness.changed();
    const targetKey=JSON.stringify(target), captured={key:targetKey,content:value,fingerprint:encoded(value)};
    latest.current.set(targetKey,captured);
    const record=records.current.get(targetKey);
    if(record) { if(record.ready) void save(captured); return; }
    const fresh={revision:0,saved:'',ready:false,blocked:false,scope:target,failure:undefined as ReadinessReason|undefined};records.current.set(targetKey,fresh);
    void api<DraftRecord>('draft_get',{...target}).then(result=>{if(deleted(target))return;fresh.revision=result.revision || 0;fresh.saved=encoded(result.content || {});fresh.ready=true;return save(latest.current.get(targetKey) || captured);}).catch(()=>{if(!deleted(target))fresh.failure='draft_save_failed';}).finally(()=>readiness.settled());
  }
  function trackDelivery(target:DraftScope,id:string){
    if(deleted(target) || !/^[a-f0-9]{64}$/.test(id))return;
    const record=records.current.get(JSON.stringify(target));if(!record)return;
    record.deliveries ||= new Set();if(!record.deliveries.has(id)){record.deliveries.add(id);readiness.changed();const captured=latest.current.get(JSON.stringify(target));if(captured)void save(captured);}
  }
  /** Only the acknowledged user-delete path may revoke these owned drafts. */
  function forgetChat(id:string){
    session.current!.deletedChats.add(id);readiness.changed();
    for(const [scopeKey,record] of records.current){if(record.scope.scope_kind==='chat' && record.scope.chat_id===id){records.current.delete(scopeKey);latest.current.delete(scopeKey);}}
    if(scope.scope_kind==='chat' && scope.chat_id===id){localRestore.current=undefined;setStatus('');setConflict(false);}
    readiness.settled();
  }
  return { key, status, conflict, discard, reloadSaved, flush: () => save(), clearSubmitted, captureScope, trackDelivery, forgetChat };
}
