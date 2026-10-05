import { useEffect, useRef, useState } from 'react';
import { ArrowLeft, ArrowRight, Globe, LoaderCircle, RotateCw } from 'lucide-react';
import { api, errorText, isNative } from './api';
import { Empty, ErrorNotice, IconButton } from './components';

interface BrowserState { url?: string; title?: string; running?: boolean; loading?: boolean; available?: boolean; message?: string; }
export function BrowserPanel() {
  const pane = useRef<HTMLDivElement>(null);
  const [state, setState] = useState<BrowserState>({}); const [address, setAddress] = useState('');
  const [editing, setEditing] = useState(false); const [error, setError] = useState('');
  useEffect(() => {
    if (!isNative() || !pane.current) return;
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
      try { await api(blocked ? 'browser_native_hide' : 'browser_native_show', blocked ? {} : data); if (!disposed) { last = key; setError(''); } }
      catch (e) { if (!disposed) setError(errorText(e)); }
      finally { pending = false; if (disposed) void api('browser_native_hide').catch(() => {}); else if (dirty) { dirty = false; schedule(); } }
    }
    function schedule() { if (disposed || frame) return; frame = requestAnimationFrame(() => { frame = 0; void bind(); }); }
    const observer = new ResizeObserver(schedule); observer.observe(pane.current); observer.observe(document.documentElement);
    const dialogs = new MutationObserver(schedule); dialogs.observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ['open', 'class', 'style', 'hidden'] });
    async function refresh() { try { const next = await api<BrowserState & { visible?: boolean }>('browser_native_status'); if (!disposed) { setState(next); if (next.running && next.visible === false && !document.querySelector('.modal-backdrop, dialog[open]')) { last = ''; schedule(); } } } catch (e) { if (!disposed) setError(errorText(e)); } }
    window.addEventListener('resize', schedule); schedule(); void refresh(); const timer = setInterval(() => void refresh(), 1500);
    return () => { disposed = true; clearInterval(timer); cancelAnimationFrame(frame); observer.disconnect(); dialogs.disconnect(); window.removeEventListener('resize', schedule); void api('browser_native_hide').catch(() => {}); };
  }, []);
  useEffect(() => { if (!editing) setAddress(state.url === 'about:blank' ? '' : state.url || ''); }, [state.url, editing]);
  async function action(name: string, data: Record<string, unknown> = {}) { setError(''); try { setState(await api<BrowserState>(`browser_native_${name}`, data)); } catch (e) { setError(errorText(e)); } }
  if (!isNative()) return <Empty icon={<Globe size={22} />} title="Open Forge desktop" text="The native browser shares this panel with your agent in the Windows app. Web research tools remain available here." />;
  return <div className="browser-pane"><div className="browser-toolbar"><IconButton label="Browser back" onClick={() => void action('back')}><ArrowLeft size={14} /></IconButton><IconButton label="Browser forward" onClick={() => void action('forward')}><ArrowRight size={14} /></IconButton><IconButton label="Reload browser" onClick={() => void action('reload')}>{state.loading ? <LoaderCircle size={14} className="spin" /> : <RotateCw size={14} />}</IconButton><form onSubmit={e => { e.preventDefault(); setEditing(false); const raw = address.trim(); if (raw) void action('navigate', { url: /^[a-z][a-z\d+.-]*:/i.test(raw) ? raw : `https://${raw}` }); }}><input aria-label="Agent browser address" placeholder="Enter a URL" value={address} onChange={e => setAddress(e.target.value)} onFocus={() => setEditing(true)} onBlur={() => setEditing(false)} /></form></div>{error && <ErrorNotice error={error} />}<div className="browser-page" id="forge-browser-panel" data-forge-browser-panel="forge-browser-panel" ref={pane}><div className="browser-placeholder"><Globe size={25} /><p>{state.running ? 'Your agent browses the same page.' : 'Starting Windows WebView2…'}</p></div></div><small className="browser-caption">{state.title || 'Forge browser'} · Agent actions follow your permissions</small></div>;
}
