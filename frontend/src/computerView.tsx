import { useEffect, useRef, useState } from 'react';
import { Activity, Monitor, RotateCw, Square, X } from 'lucide-react';
import { api } from './api';
import { Empty, IconButton } from './components';
import { updateReadiness, useDialogReadiness, useUpdateReadiness } from './updateReadiness';
import './computerView.css';

export interface ComputerViewState {
  chat_id: string; scope_run_id: string | null; mode: 'shared_desktop'; run_id: string | null;
  invocation_id: string | null; tool: string | null; status: string; action_count: number;
  action_count_truncated: boolean; evidence_mode: 'none' | 'accessibility_only' | 'image';
  retired_action_count?: number; retired_action_count_truncated?: boolean;
  snapshot: {run_id: string; invocation_id: string; data_url: string; mime_type: string; width: number; height: number; age_seconds: number | null} | null;
  failure: 'evidence_unavailable' | 'inspection_refused' | null;
}
const statusLabels: Record<string, string> = {idle: 'No desktop activity yet', queued: 'Queued', running: 'Working', compacting: 'Preparing context', reviewing: 'Reviewing', awaiting_approval: 'Waiting for approval', awaiting_question: 'Waiting for your answer', paused: 'Paused', interrupted: 'Needs inspection', failed: 'Needs attention', completed: 'Request completed', cancelled: 'Stopped', needs_attention: 'Needs attention'};
const toolLabels: Record<string, string> = {computer_windows: 'Listed apps', computer_inspect: 'Inspected desktop', computer_focus: 'Focused app', computer_click: 'Clicked target', computer_type: 'Entered text', computer_set_value: 'Set field value', computer_key: 'Pressed key', computer_coordinate_click: 'Clicked coordinates', computer_clipboard_read: 'Read clipboard', computer_clipboard_write: 'Updated clipboard'};
function valid(value: ComputerViewState, chatId: string, runId?: string) {
  return value && value.chat_id === chatId && value.scope_run_id === (runId || null) && value.mode === 'shared_desktop';
}
function age(seconds: number | null) { return seconds === null ? '' : seconds < 60 ? ` · recorded ${seconds}s ago` : seconds < 3600 ? ` · recorded ${Math.floor(seconds / 60)}m ago` : ' · saved evidence'; }
type Snapshot = NonNullable<ComputerViewState['snapshot']>;
function sameSnapshot(left:Snapshot | null | undefined, right:Snapshot) {
  return left?.run_id === right.run_id && left.invocation_id === right.invocation_id && left.data_url === right.data_url
    && left.mime_type === right.mime_type && left.width === right.width && left.height === right.height;
}
function SnapshotDialog({snapshot, close, owns}: {snapshot:Snapshot; close:()=>void; owns:()=>boolean}) {
  useDialogReadiness();
  const dialog = useRef<HTMLDialogElement>(null), [actual, setActual] = useState(false);
  useEffect(() => {
    const element = dialog.current, trigger = document.activeElement as HTMLElement | null;
    element?.showModal();
    return () => {
      const active = document.activeElement, ownedFocus = active === document.body || Boolean(element?.contains(active));
      element?.close();
      if (ownedFocus && owns() && trigger?.isConnected) trigger.focus();
    };
  }, []);
  return <dialog ref={dialog} className="modal computer-snapshot-dialog" aria-labelledby="computer-snapshot-title"
    onCancel={event => {event.preventDefault(); close();}} onClick={event => {if (event.target === event.currentTarget) close();}}>
    <header><div><h2 id="computer-snapshot-title">Earlier desktop snapshot</h2><p>Saved evidence · shared desktop</p></div><IconButton label="Close snapshot" onClick={close}><X size={18}/></IconButton></header>
    <div className="computer-snapshot-size" role="group" aria-label="Snapshot size"><button type="button" className="secondary" aria-pressed={!actual} onClick={() => setActual(false)}>Fit</button><button type="button" className="secondary" aria-pressed={actual} onClick={() => setActual(true)}>100%</button><span>{snapshot.width} × {snapshot.height}</span></div>
    <div className={`computer-snapshot-scroll ${actual ? 'actual-size' : ''}`} role="region" aria-label="Earlier snapshot image" tabIndex={0}><img src={snapshot.data_url} alt="Earlier shared-desktop snapshot enlarged"/></div>
    <p className="computer-snapshot-note">Opening this image does not capture your screen or control an app.</p>
  </dialog>;
}

export function StopComputerActivity({onStop}: {onStop?: () => void} = {}) {
  const [state, setState] = useState<'idle' | 'pending' | 'requested' | 'failed'>('idle');
  const owner = useRef(true); useEffect(() => {owner.current = true; return () => {owner.current = false;};}, []);
  async function stop() {
    if (state === 'pending') return;
    onStop?.();
    setState('pending');
    try { await api('emergency_stop'); if (owner.current) setState('requested'); }
    catch { if (owner.current) setState('failed'); }
  }
  return <div className="computer-stop"><button type="button" className="secondary" disabled={state === 'pending'} onClick={() => void stop()}><Square size={12}/>{state === 'pending' ? 'Requesting stop…' : 'Stop all agents'}</button><span role="status">{state === 'requested' ? 'Stop requested. An action already sent may finish.' : state === 'failed' ? 'Stop could not be confirmed. Use Forge’s emergency shortcut or tray menu.' : ''}</span></div>;
}

export function ComputerView({chatId, runId, visible: hostVisible = true, onActivity}: {chatId: string | null; runId?: string; visible?: boolean; onActivity: () => void}) {
  const [state, setState] = useState<ComputerViewState | null>(null), [failed, setFailed] = useState(false), [refresh, setRefresh] = useState(0);
  const [zoom, setZoom] = useState<{snapshot:Snapshot; chatId:string; runId?:string; transport:unknown} | null>(null);
  const transport = window.pywebview?.api.call || window.fetch;
  const stateTransport = useRef(transport);
  const visible = state && stateTransport.current === transport && chatId && valid(state, chatId, runId) ? state : null;
  const retainedCount = typeof visible?.retired_action_count === 'number' && Number.isInteger(visible.retired_action_count)
    && typeof visible.retired_action_count_truncated === 'boolean'
    && visible.retired_action_count > 0 && visible.retired_action_count <= 2048 && visible.retired_action_count <= visible.action_count
    ? visible.retired_action_count : 0;
  const onlyRetired = Boolean(visible && retainedCount > 0 && retainedCount === visible.action_count
    && visible.action_count_truncated === false && visible.retired_action_count_truncated === false);
  const imageOwner = useRef({chatId, runId, transport, snapshot:visible?.snapshot, visible:hostVisible});
  imageOwner.current = {chatId, runId, transport, snapshot:visible?.snapshot, visible:hostVisible};
  const ownsZoom = (captured:NonNullable<typeof zoom>) => {
    const current = imageOwner.current;
    return current.visible && document.visibilityState !== 'hidden' && current.chatId === captured.chatId && current.runId === captured.runId
      && current.transport === captured.transport && sameSnapshot(current.snapshot, captured.snapshot);
  };
  const currentZoom = zoom && ownsZoom(zoom) ? zoom : null;
  useUpdateReadiness(); const disabled = !updateReadiness.canEdit();
  useEffect(() => {if (zoom && !ownsZoom(zoom)) setZoom(null);}, [chatId, runId, transport, visible?.snapshot, hostVisible]);
  useEffect(() => {
    const hidden = () => {if (document.visibilityState === 'hidden') setZoom(null);};
    document.addEventListener('visibilitychange', hidden);
    return () => document.removeEventListener('visibilitychange', hidden);
  }, []);
  const enlarge = () => {
    if (!updateReadiness.canEdit() || !chatId || !hostVisible || !visible?.snapshot || document.visibilityState === 'hidden') return;
    setZoom({snapshot:visible.snapshot, chatId, runId, transport});
  };
  useEffect(() => {
    setState(null); setFailed(false);
    if (!chatId) return;
    let disposed = false, pending = false;
    async function read() {
      if (disposed || pending || !hostVisible || document.visibilityState === 'hidden') return;
      pending = true;
      try {
        const value = await api<ComputerViewState>('computer_view_state', {chat_id: chatId, ...(runId ? {run_id: runId} : {})});
        if (!disposed && valid(value, chatId!, runId)) {stateTransport.current = transport; setState(value); setFailed(false);}
        else if (!disposed) {setState(null); setFailed(true);}
      } catch { if (!disposed) {setState(null); setFailed(true);} }
      finally {pending = false;}
    }
    void read(); const timer = setInterval(() => void read(), 1500);
    document.addEventListener('visibilitychange', read);
    return () => {disposed = true; clearInterval(timer); document.removeEventListener('visibilitychange', read);};
  }, [chatId, runId, transport, refresh, hostVisible]);
  if (!chatId) return <Empty icon={<Monitor size={23}/>} title="Choose a conversation" text="Desktop evidence stays with the request that captured it."/>;
  return <section className="computer-view" aria-label="Desktop activity"><header><div><strong>Shared desktop</strong><p>Actions can use your Windows pointer and activate apps.</p></div><IconButton label="Refresh desktop activity" onClick={() => setRefresh(value => value + 1)}><RotateCw size={14}/></IconButton></header>
    {failed ? <p className="computer-view-error" role="alert">Desktop activity is unavailable. Refresh to try again.</p> : !visible ? <p className="muted">Loading saved activity…</p> : <>
      <p className="computer-state" role="status">{statusLabels[visible.status] || 'Needs attention'}</p>
      {visible.snapshot && /^data:image\/(?:jpeg|png);base64,[A-Za-z0-9+/=]+$/.test(visible.snapshot.data_url) ? <figure><button type="button" className="computer-snapshot-open" aria-label="Enlarge earlier snapshot" disabled={disabled} onClick={enlarge}><img src={visible.snapshot.data_url} alt="Earlier shared-desktop snapshot"/></button><figcaption>Earlier snapshot{age(visible.snapshot.age_seconds)}<span>May include other visible windows.</span></figcaption></figure> : <div className="computer-empty"><Monitor size={26}/><p>{visible.evidence_mode === 'accessibility_only' ? 'Accessibility evidence recorded. No image is available.' : visible.failure ? 'Saved snapshot is unavailable.' : retainedCount > 0 ? 'Earlier helper activity retained. No snapshot is available.' : 'No desktop snapshot yet.'}</p></div>}
      <p className="computer-evidence-note">Opening this view does not capture your screen or control an app.</p>
      {visible.action_count > 0 && <div className="computer-last-step"><span>{toolLabels[visible.tool || ''] || 'Desktop step recorded'}<small>{visible.action_count_truncated ? 'At least ' : ''}{visible.action_count} recorded {onlyRetired ? 'helper ' : ''}{visible.action_count === 1 ? 'step' : 'steps'}</small>{retainedCount > 0 && !onlyRetired && <small>Earlier helper activity · {visible.retired_action_count_truncated === true ? 'at least ' : ''}{retainedCount} {retainedCount === 1 ? 'step' : 'steps'}</small>}</span><button className="text-button" type="button" onClick={onActivity}><Activity size={13}/>Activity</button></div>}
    </>}{currentZoom && <SnapshotDialog snapshot={currentZoom.snapshot} close={() => setZoom(null)} owns={() => ownsZoom(currentZoom)}/>}
  </section>;
}
