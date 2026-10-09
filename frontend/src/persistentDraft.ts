import { useEffect, useRef, useState } from 'react';
import { api, errorText } from './api';

export interface DraftScope { scope_kind: 'chat' | 'new_chat' | 'builder'; project_id?: string | null; chat_id?: string; builder_id?: string; }
export interface DraftContent { text?: string; document_ids?: string[]; skills?: string[]; space_ids?: string[]; brief?: Record<string, unknown>; base_revision?: number; }
interface DraftRecord { revision: number; content: DraftContent; stale_refs?: unknown[]; }
const encoded = (value: DraftContent) => JSON.stringify(value);

/** CAS drafts are local to Forge. Images deliberately never enter this store. */
export function usePersistentDraft(scope: DraftScope, content: DraftContent, restore: (content: DraftContent) => void, enabled = true) {
  const key = JSON.stringify(scope), fingerprint = encoded(content);
  const current = useRef({ key, content, fingerprint }); current.current = { key, content, fingerprint };
  const latest = useRef(new Map<string, typeof current.current>()); latest.current.set(key, current.current);
  const records = useRef(new Map<string, { revision: number; saved: string; ready: boolean; blocked: boolean; scope: DraftScope }>());
  const queue = useRef(Promise.resolve());
  const [status, setStatus] = useState(''), [conflict, setConflict] = useState(false);
  const restoreRef = useRef(restore); restoreRef.current = restore;
  function enqueue(operation: () => Promise<void>) { queue.current = queue.current.catch(() => {}).then(operation); return queue.current; }
  async function save(captured = current.current) {
    const record = records.current.get(captured.key);
    if (!record?.ready || record.blocked || captured.fingerprint === record.saved) return;
    await enqueue(async () => {
      if (record.blocked || captured.fingerprint === record.saved) return;
      // A queued old revision cannot rewrite a newer edit or a cleared draft.
      if (latest.current.get(captured.key)?.fingerprint!==captured.fingerprint) return;
      try {
        const result = await api<DraftRecord>('draft_save', { ...record.scope, expected_revision: record.revision, content: captured.content });
        record.revision = result.revision; record.saved = captured.fingerprint;
        if (current.current.key === captured.key) { setStatus(result.stale_refs?.length ? 'Draft saved locally; some references are unavailable' : 'Draft saved locally'); setConflict(false); }
      } catch (error) {
        record.blocked = true;
        if (current.current.key === captured.key) { setStatus(`Draft kept in this window: ${errorText(error)}`); setConflict(true); }
      }
    });
  }
  useEffect(() => {
    if (!enabled) return;
    setStatus(''); setConflict(false);
    if (records.current.has(key)) return () => { const captured = latest.current.get(key); if (captured) void save(captured); };
    const initial = current.current.fingerprint;
    const initialContent=current.current.content;
    const hadLocalWork=scope.scope_kind!=='builder' && Boolean(initialContent.text || initialContent.document_ids?.length || initialContent.skills?.length || initialContent.space_ids?.length);
    const record = { revision: 0, saved: initial, ready: false, blocked: false, scope }; records.current.set(key, record);
    void api<DraftRecord>('draft_get', {...scope}).then(result => {
      record.revision = result.revision || 0; record.ready = true;
      const restored = result.content || {};
      // Typing and attachment changes during restoration always win locally.
      if (!hadLocalWork && current.current.key === key && current.current.fingerprint === initial && Object.keys(restored).length) {
        record.saved = encoded(restored); restoreRef.current(restored);
        setStatus(result.stale_refs?.length ? 'Draft restored; some references are unavailable' : 'Draft restored locally');
      } else { record.saved = encoded(restored); void save(latest.current.get(key) || current.current); }
    }).catch(error => { records.current.delete(key); if (current.current.key === key) setStatus(`Draft restoration unavailable: ${errorText(error)}`); });
    // The prior key is flushed before navigation/unmount; captured content avoids
    // saving a new chat's draft into the previous chat.
    return () => { const captured = latest.current.get(key); if (captured) void save(captured); };
  }, [key, enabled]);
  useEffect(() => { if (!enabled) return; const captured = { key, content, fingerprint }; const timer = setTimeout(() => void save(captured), 500); return () => clearTimeout(timer); }, [key, fingerprint, enabled]);
  async function discard() {
    const captured = current.current, record = records.current.get(captured.key); if (!record) return;
    await enqueue(async () => {
      try {
        // Discarding this window's conflicting edits preserves the other
        // window's saved version. Never rebase a delete onto its newer revision.
        if (record.blocked) {
          const latest = await api<DraftRecord>('draft_get', {...record.scope});
          record.revision = latest.revision; record.saved = encoded(latest.content || {}); record.blocked = false;
          if (current.current.key === captured.key) {restoreRef.current(latest.content || {});setConflict(false);setStatus('Local edits discarded; latest saved draft restored');}
          return;
        }
        const result = await api<DraftRecord>('draft_clear', { ...record.scope, expected_revision: record.revision });
        record.revision = result.revision; record.saved = encoded({}); record.blocked = false;
        if(latest.current.get(captured.key)?.fingerprint===captured.fingerprint){latest.current.set(captured.key,{...captured,content:{},fingerprint:encoded({})});if(current.current.key===captured.key){current.current={...captured,content:{},fingerprint:encoded({})};restoreRef.current({});setConflict(false);setStatus('Draft discarded');}}
      } catch (error) { setStatus(errorText(error)); setConflict(true); }
    });
  }
  async function reloadSaved() {
    const captured=current.current, record=records.current.get(captured.key); if(!record)return;
    await enqueue(async()=>{try{const result=await api<DraftRecord>('draft_get',{...record.scope});record.revision=result.revision;record.saved=encoded(result.content||{});record.ready=true;record.blocked=false;if(current.current.key===captured.key){restoreRef.current(result.content||{});setConflict(false);setStatus('Latest saved draft restored');}}catch(error){if(current.current.key===captured.key)setStatus(errorText(error));}});
  }
  async function clearSubmitted(scopeKey: string, submitted: string) {
    await enqueue(async () => {
      const record = records.current.get(scopeKey);
      if (!record?.ready || record.blocked || record.saved !== submitted) return;
      const local = latest.current.get(scopeKey);
      if (local && local.fingerprint !== submitted && local.fingerprint !== encoded({})) return;
      try {
        const result = await api<DraftRecord>('draft_clear', {...record.scope, expected_revision: record.revision});
        record.revision = result.revision; record.saved = encoded({});
      } catch (error) { record.blocked = true; if (current.current.key === scopeKey) {setConflict(true); setStatus(`Submitted; saved draft needs reconciliation: ${errorText(error)}`);} }
    });
  }
  return { key, status, conflict, discard, reloadSaved, flush: () => save(), clearSubmitted };
}
