import { useEffect, useLayoutEffect, useRef, useSyncExternalStore } from 'react';

export type ReadinessReason = 'workspace_loading' | 'draft_restoring' | 'draft_conflict' | 'draft_save_failed' | 'draft_references' | 'draft_scope_unavailable' | 'unsent_images' | 'attachment_pending' | 'dictation_pending' | 'submission_pending' | 'submission_unknown' | 'active_work' | 'approval_pending' | 'question_pending' | 'recovery_pending' | 'dialog_open' | 'connection_pending' | 'unsaved_setup' | 'builder_scratch' | 'preferences_pending' | 'native_input' | 'workspace_changed' | 'confirmation_expired' | 'helper_failed' | 'preparation_failed' | 'protocol_error' | 'handoff_unavailable' | 'paired_editor';
export type ReadinessPhase = 'idle' | 'checking' | 'locked' | 'prepared' | 'aborting' | 'failed';
export interface ReadinessObservation { reasons: readonly ReadinessReason[]; pending?: number; }
export type SaveConfirmation = { ok: true; receipts: string[] } | { ok: false; reason: ReadinessReason };
export interface ReadinessParticipant {
  inspect(): ReadinessObservation;
  flushAndConfirm(context: { attemptId: string; signal: AbortSignal }): Promise<SaveConfirmation>;
  retain?(): boolean;
}
export interface ReadinessSnapshot { phase: ReadinessPhase; revision: number; reason?: ReadinessReason; }
export const readinessMessages: Record<ReadinessReason, string> = {
  workspace_loading: 'Wait for your workspace and saved drafts to load.', draft_restoring: 'Wait for your saved drafts to load.',
  draft_conflict: 'A draft changed in another window. Reconcile it before restarting.',
  draft_save_failed: 'Your latest changes could not be saved. Keep Forge open and retry.',
  draft_references: 'A draft needs attention before restarting.', draft_scope_unavailable: 'A draft needs attention before restarting.',
  unsent_images: 'Send or remove unsent images before restarting.', attachment_pending: 'Wait for your attachments to finish.',
  dictation_pending: 'Finish or cancel dictation before restarting.', submission_pending: 'Wait for active work to finish before restarting.',
  submission_unknown: 'Inspect an interrupted action before restarting.', active_work: 'Wait for active work to finish before restarting.',
  approval_pending: 'Forge is waiting for your input.', question_pending: 'Forge is waiting for your input.',
  recovery_pending: 'Inspect an interrupted action before restarting.', dialog_open: 'Finish or close the open dialog before restarting.',
  connection_pending: 'Wait for the connection setup to finish.', unsaved_setup: 'Finish or close the open setup before restarting.',
  builder_scratch: 'Save or discard your unsaved Builder work before restarting.', preferences_pending: 'Your latest preferences need to finish saving.',
  native_input: 'Finish your browser or preview work before restarting.', workspace_changed: 'Your workspace changed. Check again when you are ready.',
  confirmation_expired: 'The restart check expired. Check again.',
  helper_failed:'The update helper could not start safely. Keep Forge open and retry.',
  preparation_failed:'The update could not be prepared. Keep Forge open and retry.',
  protocol_error:'The restart could not be confirmed. Keep Forge open and check again.',
  handoff_unavailable:'In-app restart is unavailable. Save your work before installing manually.',
  paired_editor:'Save your work in paired browsers, then disconnect them before restarting.',
};

/** This registry holds callbacks and counters only. Owners retain all private work. */
export class ReadinessRegistry {
  private owners = new Map<number, { participant: ReadinessParticipant; detached: boolean }>();
  private listeners = new Set<() => void>();
  private sequence = 0;
  private snapshot: ReadinessSnapshot = { phase: 'idle', revision: 0 };
  private attempt?: { id: string; revision: number; abort: AbortController };
  private invalidated?: () => void;
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener); }; };
  getSnapshot = () => this.snapshot;
  participantCount() { return this.owners.size; }
  fence(attemptId:string,revision:number,count:number) { return this.unchanged(attemptId,revision) && this.owners.size===count && ![...this.owners.values()].some(owner=>owner.participant.inspect().reasons.length); }
  private publish(patch: Partial<ReadinessSnapshot>) { this.snapshot = { ...this.snapshot, ...patch }; this.listeners.forEach(listener => listener()); }
  register(participant: ReadinessParticipant) {
    const id = ++this.sequence; this.owners.set(id, { participant, detached: false }); this.changed();
    return { changed: () => this.changed(), settled: () => this.releaseSettled(id,true), detach: () => {
      const owner = this.owners.get(id); if (!owner) return; owner.detached = true; this.releaseSettled(id);
    }, attach:()=>{const owner=this.owners.get(id);if(owner){owner.detached=false;this.changed();return true;}return false;} };
  }
  private releaseSettled(id: number,deferDuringAttempt=false) {
    const owner = this.owners.get(id);
    // Expected flushing cannot change the acknowledged participant membership.
    // A detached controller stays available until this attempt is resolved.
    if(deferDuringAttempt && this.attempt)return;
    if (owner?.detached && !owner.participant.retain?.()) { this.owners.delete(id); this.changed(); }
  }
  changed() {
    this.publish({ revision: this.snapshot.revision + 1 });
    if (this.attempt && !this.attempt.abort.signal.aborted) {
      this.attempt.abort.abort(); this.publish({ phase: 'aborting', reason: 'workspace_changed' }); this.invalidated?.();
    }
  }
  isLocked() { return ['checking', 'locked', 'prepared', 'aborting'].includes(this.snapshot.phase); }
  canEdit() { return !this.isLocked(); }
  start(attemptId: string, onInvalidated: () => void) {
    if (this.isLocked() || !attemptId || attemptId.length > 256) return false;
    this.invalidated = onInvalidated;
    this.attempt = { id: attemptId, revision: this.snapshot.revision, abort: new AbortController() };
    this.publish({ phase: 'checking', reason: undefined }); return true;
  }
  async confirm(attemptId: string, timeoutMs = 30000): Promise<SaveConfirmation & { revision?: number; participantCount?: number }> {
    const attempt = this.attempt;
    if (!attempt || attempt.id !== attemptId || attempt.abort.signal.aborted) return { ok: false, reason: 'workspace_changed' };
    const owners = [...this.owners.values()];
    if (owners.length > 512) return { ok: false, reason: 'workspace_changed' };
    const first = owners.flatMap(owner => owner.participant.inspect().reasons)[0];
    if (first) return { ok: false, reason: first };
    let timer: ReturnType<typeof setTimeout> | undefined;
    const deadline = new Promise<SaveConfirmation>(resolve => { timer = setTimeout(() => { attempt.abort.abort(); resolve({ ok: false, reason: 'confirmation_expired' }); }, timeoutMs); });
    const save = (async (): Promise<SaveConfirmation> => {
      const receipts: string[] = [];
      for (const owner of owners) {
        if (attempt.abort.signal.aborted) return { ok: false, reason: 'workspace_changed' };
        let result: SaveConfirmation;
        try { result = await owner.participant.flushAndConfirm({ attemptId, signal: attempt.abort.signal }); }
        catch { return { ok: false, reason: 'draft_save_failed' }; }
        if (!result.ok) return result;
        if (result.receipts.some(receipt => typeof receipt !== 'string' || !/^[a-f0-9]{64}$/.test(receipt)) || receipts.length + result.receipts.length > 512) return { ok: false, reason: 'draft_save_failed' };
        receipts.push(...result.receipts);
      }
      return { ok: true, receipts };
    })();
    const result = await Promise.race([save, deadline]); if (timer) clearTimeout(timer);
    if (result.ok && (this.attempt !== attempt || attempt.abort.signal.aborted || attempt.revision !== this.snapshot.revision)) return { ok: false, reason: 'workspace_changed' };
    if (result.ok) {
      const reason = [...this.owners.values()].flatMap(owner => owner.participant.inspect().reasons)[0];
      if (reason) return { ok: false, reason };
      this.publish({ phase: 'locked' }); return { ...result, revision: attempt.revision, participantCount: owners.length };
    }
    return result;
  }
  unchanged(attemptId: string, revision: number) { return this.attempt?.id === attemptId && !this.attempt.abort.signal.aborted && this.snapshot.revision === revision && ['locked', 'prepared'].includes(this.snapshot.phase); }
  prepared(attemptId: string, revision: number) { if (!this.unchanged(attemptId, revision)) return false; this.publish({ phase: 'prepared' }); return true; }
  aborting(reason: ReadinessReason) { this.attempt?.abort.abort(); this.publish({ phase: 'aborting', reason }); }
  requestAbort() { if(this.attempt){this.aborting('workspace_changed');this.invalidated?.();} }
  blocked(reason:ReadinessReason) { if(!this.attempt)this.publish({phase:'failed',reason}); }
  dismiss() { if(!this.attempt)this.publish({phase:'idle',reason:undefined}); }
  /** Call only after the native host confirms no helper can proceed. */
  aborted(attemptId: string, reason?: ReadinessReason) {
    if (this.attempt?.id !== attemptId) return false;
    this.attempt.abort.abort(); this.attempt = undefined; this.invalidated = undefined;
    this.publish({ phase: reason ? 'failed' : 'idle', reason });
    for(const id of this.owners.keys())this.releaseSettled(id);
    return true;
  }
}
export const updateReadiness = new ReadinessRegistry();
declare global { interface Window { __forgeUpdateReadiness?: { check(input:{attempt_id:string;workspace_revision:number;participant_count:number}):{ok:boolean;workspace_revision:number;participant_count:number;phase:ReadinessPhase} }; } }
export function installReadinessFence() {
  const fence={check:(input:{attempt_id:string;workspace_revision:number;participant_count:number})=>({ok:Boolean(input && updateReadiness.fence(input.attempt_id,input.workspace_revision,input.participant_count)),workspace_revision:updateReadiness.getSnapshot().revision,participant_count:updateReadiness.participantCount(),phase:updateReadiness.getSnapshot().phase})};
  window.__forgeUpdateReadiness=fence;return ()=>{if(window.__forgeUpdateReadiness===fence)delete window.__forgeUpdateReadiness;};
}
export const useUpdateReadiness = () => useSyncExternalStore(updateReadiness.subscribe, updateReadiness.getSnapshot);
export interface RetainedRegistration { latest:{current:ReadinessParticipant};handle:{current:ReturnType<ReadinessRegistry['register']>|null}; }
export function useReadinessParticipant(participant: ReadinessParticipant, retained?:RetainedRegistration) {
  const localLatest = useRef(participant),localHandle = useRef<ReturnType<ReadinessRegistry['register']> | null>(null);
  const latest=retained?.latest || localLatest,handle=retained?.handle || localHandle;latest.current=participant;
  useLayoutEffect(() => {if(!handle.current?.attach())handle.current = updateReadiness.register({ inspect: () => latest.current.inspect(), flushAndConfirm: context => latest.current.flushAndConfirm(context), retain: () => latest.current.retain?.() || false });const captured=handle.current;return () => { captured.detach(); }; }, []);
  return { changed: () => handle.current?.changed(), settled: () => handle.current?.settled() };
}
export function useDialogReadiness() {
  useLayoutEffect(() => { const handle = updateReadiness.register({ inspect: () => ({ reasons: ['dialog_open'] }), flushAndConfirm: async () => ({ ok: false, reason: 'dialog_open' }) }); return handle.detach; }, []);
}
