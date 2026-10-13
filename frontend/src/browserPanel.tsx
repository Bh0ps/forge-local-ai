import { useEffect, useRef, useState } from 'react';
import { ArrowLeft, ArrowRight, Globe, LoaderCircle, RotateCw } from 'lucide-react';
import { api, errorText, isNative } from './api';
import { Empty, ErrorNotice, IconButton } from './components';
import { updateReadiness, useUpdateReadiness } from './updateReadiness';

interface BrowserState { url?: string; title?: string; running?: boolean; loading?: boolean; available?: boolean; message?: string; }
const pageAddress = (state: BrowserState) => state.url === 'about:blank' ? '' : state.url || '';
export function BrowserPanel() {
  const pane = useRef<HTMLDivElement>(null);
  const [state, setState] = useState<BrowserState>({}); const [address, setAddress] = useState('');
  const [pending, setPending] = useState(false);
  const [actionError, setActionError] = useState(''); const [statusError, setStatusError] = useState(''); const [bindError, setBindError] = useState('');
  const currentState = useRef<BrowserState>({}); const request = useRef(0); const actionPending = useRef(false);
  const lifetime = useRef<object | null>(null); const input = useRef({ focused: false, dirty: false, revision: 0 });
  useUpdateReadiness(); const disabled = !updateReadiness.canEdit();
  function acceptState(next: BrowserState) {
    currentState.current = next; setState(next);
    if (!input.current.focused && !input.current.dirty) setAddress(pageAddress(next));
  }
  useEffect(() => {
    if (!isNative() || !pane.current) return;
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
      try { await api(blocked ? 'browser_native_hide' : 'browser_native_show', blocked ? {} : data); if (!disposed) { last = key; setBindError(''); } }
      catch (e) { if (!disposed) setBindError(errorText(e)); }
      finally { pending = false; if (disposed) void api('browser_native_hide').catch(() => {}); else if (dirty) { dirty = false; schedule(); } }
    }
    function schedule() { if (disposed || frame) return; frame = requestAnimationFrame(() => { frame = 0; void bind(); }); }
    const observer = new ResizeObserver(schedule); observer.observe(pane.current); observer.observe(document.documentElement);
    const dialogs = new MutationObserver(schedule); dialogs.observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ['open', 'class', 'style', 'hidden'] });
    async function refresh() {
      if (disposed || actionPending.current) return;
      const identity = ++request.current;
      try {
        const next = await api<BrowserState & { visible?: boolean }>('browser_native_status');
        if (!disposed && identity === request.current) {
          acceptState(next); setStatusError('');
          if (next.running && next.visible === false && !document.querySelector('.modal-backdrop, dialog[open]')) { last = ''; schedule(); }
        }
      } catch (e) { if (!disposed && identity === request.current) setStatusError(errorText(e)); }
    }
    window.addEventListener('resize', schedule); schedule(); void refresh(); const timer = setInterval(() => void refresh(), 1500);
    return () => { disposed = true; if (lifetime.current === owner) lifetime.current = null; clearInterval(timer); cancelAnimationFrame(frame); observer.disconnect(); dialogs.disconnect(); window.removeEventListener('resize', schedule); void api('browser_native_hide').catch(() => {}); };
  }, []);
  async function action(name: string, data: Record<string, unknown> = {}) {
    const owner = lifetime.current;
    if (!owner || !updateReadiness.canEdit()) return;
    const identity = ++request.current; const revision = input.current.revision;
    actionPending.current = true; setPending(true); setActionError('');
    try {
      const next = await api<BrowserState>(`browser_native_${name}`, data);
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
