import { useEffect, useRef, useState } from 'react';
import { ArrowLeft, ArrowRight, Globe, LoaderCircle, RotateCw } from 'lucide-react';
import { api, errorText, isNative } from './api';
import { Empty, ErrorNotice, IconButton } from './components';
import { updateReadiness, useUpdateReadiness } from './updateReadiness';
import { ComputerView, StopComputerActivity } from './computerView';
import type { BrowserMode } from './workspaceLayout';

interface BrowserState { url?: string; title?: string; running?: boolean; loading?: boolean; available?: boolean; message?: string; }
const pageAddress = (state: BrowserState) => state.url === 'about:blank' ? '' : state.url || '';
async function hideBrowser() {
  const result = await api<{ok?: boolean}>('browser_native_hide');
  if (result?.ok !== true) throw new Error('Browser hide was not confirmed.');
  return result;
}
export function BrowserPanel({chatId = null, runId, projectId, visible = true, onActivity = () => {}, browserMode,
  preferenceReady = true, preferenceError = null, preferenceRetrying = false, onPreferenceRetry, onModeChange}:
  {chatId?: string | null; runId?: string; projectId?: string | null; visible?: boolean; onActivity?: () => void;
   browserMode?: BrowserMode; preferenceReady?: boolean; preferenceError?: 'load'|'save'|null; preferenceRetrying?: boolean;
   onPreferenceRetry?: () => Promise<boolean>; onModeChange?: (mode:BrowserMode) => void} = {}) {
  const [mode, setMode] = useState<'loading' | 'browser' | 'hiding' | 'desktop' | 'hide_failed'>('loading');
  const [localChoice, setLocalChoice] = useState<BrowserMode>('browser');
  const choice = browserMode ?? localChoice;
  const transport = window.pywebview?.api.call || window.fetch;
  const transportRef = useRef(transport), modeRef = useRef(mode), nativeOwned = useRef(false);
  transportRef.current = transport; modeRef.current = mode;
  const mounted = useRef(true), wanted = useRef(false), transition = useRef(0);
  const previousRelease = useRef<Promise<unknown>>(Promise.resolve());
  useUpdateReadiness(); const disabled = !updateReadiness.canEdit();
  useEffect(() => {mounted.current = true; return () => {mounted.current = false;};}, []);
  function confirmDesktop(drain: Promise<unknown>) {
    const identity = transition.current, bridge = transportRef.current;
    const current = () => mounted.current && wanted.current && identity === transition.current && bridge === transportRef.current
      && bridge === (window.pywebview?.api.call || window.fetch);
    // A retired pane only supplies its drain. Desktop needs a fresh strict
    // Hide from the transport that owns this transition after that drain.
    void drain.catch(() => {}).then(async () => {
      if (!current()) return;
      try {await hideBrowser(); if (current()) setMode('desktop');}
      catch {if (current()) setMode('hide_failed');}
    });
  }
  function released(hidden: Promise<unknown>, admittedBridge:unknown) {
    previousRelease.current = hidden;
    if (!wanted.current) return;
    if (admittedBridge !== transportRef.current) {confirmDesktop(hidden); return;}
    const identity = transition.current;
    const current = () => mounted.current && wanted.current && identity === transition.current && admittedBridge === transportRef.current
      && admittedBridge === (window.pywebview?.api.call || window.fetch);
    void hidden.then(() => {if (current()) setMode('desktop');}, () => {if (current()) setMode('hide_failed');});
  }
  useEffect(() => {
    transition.current++;
    if (!preferenceReady) {wanted.current = false; setMode('loading'); return;}
    wanted.current = choice === 'desktop';
    if (choice === 'browser') {setMode('browser'); return;}
    if (!isNative()) {setMode('desktop'); return;}
    setMode('hiding');
    // A mounted browser owns its outstanding effects and final Hide. A restored
    // Desktop preference never mounts it, but still confirms the current Hide.
    if (modeRef.current !== 'browser' || !nativeOwned.current) {
      confirmDesktop(previousRelease.current);
    }
  }, [choice, preferenceReady, transport]);
  function choose(next:BrowserMode) {
    if (!preferenceReady || !updateReadiness.canEdit() || mode === 'hiding' || mode === 'hide_failed') return;
    if (onModeChange) onModeChange(next); else setLocalChoice(next);
  }
  const waiting = !preferenceReady || mode === 'loading';
  return <div className="computer-browser-view"><div className="computer-view-modes" role="group" aria-label="Workspace browser mode"><button type="button" aria-pressed={mode === 'browser' && !waiting} disabled={disabled || waiting || mode === 'hiding' || mode === 'hide_failed'} onClick={() => choose('browser')}>Browser</button><button type="button" aria-pressed={choice === 'desktop' && !waiting} disabled={disabled || waiting || mode === 'hiding' || mode === 'hide_failed'} onClick={() => choose('desktop')}>Desktop</button></div>
    {waiting ? <div className="computer-hide-state"><p role={preferenceError === 'load' ? 'alert' : 'status'}>{preferenceError === 'load' ? 'The workspace view preference could not be loaded.' : 'Loading workspace view…'}</p>{preferenceError === 'load' && onPreferenceRetry && <button type="button" className="secondary" disabled={disabled || preferenceRetrying} onClick={() => void onPreferenceRetry()}>{preferenceRetrying ? 'Retrying…' : 'Retry view preference'}</button>}</div> : mode === 'browser' ? <NativeBrowserPanel visible={visible} onReleased={released} onNativeOwned={owned => {nativeOwned.current = owned;}}/> : mode === 'desktop' ? <ComputerView key={`view:${projectId || ''}:${chatId || ''}:${runId || ''}`} chatId={chatId} runId={runId} visible={visible} onActivity={onActivity}/> : <div className="computer-hide-state"><p role="status">{mode === 'hiding' ? 'Hiding the browser…' : 'The browser could not be hidden. Retry switching to Desktop.'}</p>{mode === 'hide_failed' && <button className="secondary" type="button" disabled={disabled} onClick={() => {setMode('hiding'); confirmDesktop(previousRelease.current);}}>Retry switching</button>}</div>}
    {mode !== 'browser' && <StopComputerActivity key={`stop:${projectId || ''}:${chatId || ''}:${runId || ''}`}/>}
  </div>;
}

function NativeBrowserPanel({onReleased, onNativeOwned, visible}: {onReleased: (hidden: Promise<unknown>, admittedBridge:unknown) => void; onNativeOwned: (owned:boolean) => void; visible: boolean}) {
  const pane = useRef<HTMLDivElement>(null);
  const [state, setState] = useState<BrowserState>({}); const [address, setAddress] = useState('');
  const [pending, setPending] = useState(false);
  const [actionError, setActionError] = useState(''); const [statusError, setStatusError] = useState(''); const [bindError, setBindError] = useState('');
  const currentState = useRef<BrowserState>({}); const request = useRef(0); const actionPending = useRef(false);
  const lifetime = useRef<object | null>(null); const input = useRef({ focused: false, dirty: false, revision: 0 });
  const effects = useRef(new Set<Promise<unknown>>());
  const hostVisible = useRef(visible); hostVisible.current = visible;
  async function tracked<T>(name: string, data: Record<string, unknown> = {}): Promise<T> {
    const work = api<T>(name, data); effects.current.add(work);
    try {return await work;} finally {effects.current.delete(work);}
  }
  useUpdateReadiness(); const disabled = !updateReadiness.canEdit();
  function acceptState(next: BrowserState) {
    currentState.current = next; setState(next);
    if (!input.current.focused && !input.current.dirty) setAddress(pageAddress(next));
  }
  useEffect(() => {
    if (!isNative() || !pane.current) return;
    const admittedBridge = window.pywebview?.api.call || window.fetch;
    onNativeOwned(true);
    const owner = {}; lifetime.current = owner;
    let disposed = false; let frame = 0; let pending = false; let dirty = false; let last = '';
    async function bind() {
      if (disposed || !pane.current) return;
      if (pending) { dirty = true; return; }
      const blocked = Boolean(document.querySelector('.modal-backdrop, dialog[open]'));
      const rect = pane.current.getBoundingClientRect();
      const data = { panel_id: 'forge-browser-panel', rect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height }, viewport: { width: innerWidth, height: innerHeight } };
      const key = blocked ? 'hidden' : JSON.stringify(data);
      if (last === key || rect.width < 10 || rect.height < 10) return;
      pending = true;
      try { await tracked(blocked ? 'browser_native_hide' : 'browser_native_show', blocked ? {} : data); if (!disposed) { last = key; setBindError(''); } }
      catch (e) { if (!disposed) setBindError(errorText(e)); }
      finally { pending = false; if (!disposed && dirty) { dirty = false; schedule(); } }
    }
    function schedule() { if (disposed || frame) return; frame = requestAnimationFrame(() => { frame = 0; void bind(); }); }
    const observer = new ResizeObserver(schedule); observer.observe(pane.current); observer.observe(document.documentElement);
    const dialogs = new MutationObserver(schedule); dialogs.observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ['open', 'class', 'style', 'hidden'] });
    async function refresh() {
      if (disposed || actionPending.current || !hostVisible.current || document.visibilityState === 'hidden') return;
      const identity = ++request.current;
      try {
        const next = await api<BrowserState & { visible?: boolean }>('browser_native_status');
        if (!disposed && identity === request.current) {
          acceptState(next); setStatusError('');
          if (next.running && next.visible === false && !document.querySelector('.modal-backdrop, dialog[open]')) { last = ''; schedule(); }
        }
      } catch (e) { if (!disposed && identity === request.current) setStatusError(errorText(e)); }
    }
    window.addEventListener('resize', schedule); document.addEventListener('visibilitychange', refresh); schedule(); void refresh(); const timer = setInterval(() => void refresh(), 1500);
    return () => {
      onNativeOwned(false);
      disposed = true; if (lifetime.current === owner) lifetime.current = null;
      clearInterval(timer); cancelAnimationFrame(frame); observer.disconnect(); dialogs.disconnect(); window.removeEventListener('resize', schedule); document.removeEventListener('visibilitychange', refresh);
      // Await every admitted browser effect before the final hide; otherwise a
      // late Show/navigation can cover a newly mounted desktop evidence view.
      const outstanding = [...effects.current];
      const firstHide = hideBrowser();
      const hidden = outstanding.length ? Promise.allSettled([...outstanding, firstHide]).then(hideBrowser) : firstHide;
      onReleased(hidden, admittedBridge); void hidden.catch(() => {});
    };
  }, []);
  async function action(name: string, data: Record<string, unknown> = {}) {
    const owner = lifetime.current;
    if (!owner || !updateReadiness.canEdit()) return;
    const identity = ++request.current; const revision = input.current.revision;
    actionPending.current = true; setPending(true); setActionError('');
    try {
      const next = await tracked<BrowserState>(`browser_native_${name}`, data);
      if (lifetime.current !== owner || identity !== request.current) return;
      currentState.current = next; setState(next); setStatusError('');
      // A submitted address belongs to this action until it settles. Later edits
      // remain private input, including when the user moves focus elsewhere.
      if (revision === input.current.revision && (name === 'navigate' || !input.current.focused)) {
        input.current.dirty = false; setAddress(next.url === undefined && name === 'navigate' ? String(data.url) : pageAddress(next));
      }
    } catch (e) { if (lifetime.current === owner && identity === request.current) setActionError(errorText(e)); }
    finally { if (lifetime.current === owner && identity === request.current) { actionPending.current = false; setPending(false); } }
  }
  if (!isNative()) return <Empty icon={<Globe size={22} />} title="Open Forge desktop" text="The native browser shares this panel with your agent in the Windows app. Web research tools remain available here." />;
  const error = actionError || statusError || bindError;
  return <div className="browser-pane"><div className="browser-toolbar"><IconButton label="Browser back" disabled={disabled} onClick={() => void action('back')}><ArrowLeft size={14} /></IconButton><IconButton label="Browser forward" disabled={disabled} onClick={() => void action('forward')}><ArrowRight size={14} /></IconButton><IconButton label="Reload browser" disabled={disabled} onClick={() => void action('reload')}>{pending || state.loading ? <LoaderCircle size={14} className="spin" /> : <RotateCw size={14} />}</IconButton><form onSubmit={e => {
    e.preventDefault(); if (!updateReadiness.canEdit()) return;
    const raw = address.trim(); if (!raw) return;
    const url = /^[a-z][a-z\d+.-]*:/i.test(raw) ? raw : `https://${raw}`;
    input.current.dirty = true; setAddress(url); void action('navigate', { url });
  }}><input aria-label="Agent browser address" placeholder="Enter a URL" value={address} disabled={disabled} onChange={e => {
    if (!updateReadiness.canEdit()) return;
    input.current.revision++; input.current.dirty = true; setAddress(e.target.value);
  }} onFocus={() => { input.current.focused = true; }} onBlur={() => {
    input.current.focused = false;
    if (!input.current.dirty && !actionPending.current) setAddress(pageAddress(currentState.current));
  }} /></form></div>{error && <ErrorNotice error={error} />}<div className="browser-page" id="forge-browser-panel" data-forge-browser-panel="forge-browser-panel" ref={pane}><div className="browser-placeholder"><Globe size={25} /><p>{state.running ? 'Your agent browses the same page.' : 'Starting Windows WebView2…'}</p></div></div><small className="browser-caption">{state.title || 'Forge browser'} · Agent actions follow your permissions</small></div>;
}
