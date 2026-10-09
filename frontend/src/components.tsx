import { cloneElement, isValidElement, useEffect, useId, useRef, useState } from 'react';
import { Check, ChevronDown, Copy, Info, Search, X, Eye, Wrench, LoaderCircle, ArrowUpRight } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import { api, bytesLabel, friendlyModel, isNative, native, tokenLabel } from './api';
import type { Message, Model, RunEvent } from './types';

export function IconButton({ label, children, onClick, disabled, active, className = '' }: { label: string; children: React.ReactNode; onClick?: () => void; disabled?: boolean; active?: boolean; className?: string }) {
  return <button type="button" className={`icon-button ${active ? 'selected' : ''} ${className}`} aria-label={label} title={label} aria-pressed={active} onClick={onClick} disabled={disabled}>{children}</button>;
}
export function Modal({ title, children, close, wide = false }: { title: string; children: React.ReactNode; close: () => void; wide?: boolean }) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => { const trigger = document.activeElement as HTMLElement | null; ref.current?.showModal(); return () => {ref.current?.close(); if (trigger?.isConnected) trigger.focus();}; }, []);
  return <dialog ref={ref} className={`modal ${wide ? 'wide' : ''}`} onCancel={close} onClick={e => { if (e.target === e.currentTarget) close(); }}><header><h2>{title}</h2><IconButton label="Close dialog" onClick={close}><X size={18} /></IconButton></header>{children}</dialog>;
}
export function Field({ label, children, hint }: { label: string; children: React.ReactNode; hint?: string }) {
  const id = useId();
  const control = isValidElement<Record<string, unknown>>(children) && typeof children.type === 'string' && ['input', 'select', 'textarea'].includes(children.type);
  return <div className="field">{control ? <label htmlFor={id}>{label}</label> : <span>{label}</span>}{control ? cloneElement(children as React.ReactElement<Record<string, unknown>>, { id, 'aria-describedby': hint ? `${id}-hint` : undefined }) : children}{hint && <small id={`${id}-hint`}>{hint}</small>}</div>;
}
export function Empty({ icon, title, text, action }: { icon?: React.ReactNode; title: string; text?: string; action?: React.ReactNode }) { return <div className="empty-state">{icon && <div className="empty-icon">{icon}</div>}<h2>{title}</h2>{text && <p>{text}</p>}{action}</div>; }
export function Badge({ children, tone = '' }: { children: React.ReactNode; tone?: string }) { return <span className={`badge ${tone}`}>{children}</span>; }
export function ErrorNotice({ error, retry }: { error: string; retry?: () => void }) { return <div className="inline-error" role="alert"><span>{error}</span>{retry && <button onClick={retry}>Retry</button>}</div>; }
export function PageHeader({ title, subtitle, children }: { title: string; subtitle?: string; children?: React.ReactNode }) { return <div className="page-header"><div><h1>{title}</h1>{subtitle && <p>{subtitle}</p>}</div><div className="page-actions">{children}</div></div>; }
export function Toggle({ label, checked, onChange, description, disabled }: { label: string; checked: boolean; onChange: (v: boolean) => void; description?: string; disabled?: boolean }) {
  return <div className="setting-row"><div><span>{label}</span>{description && <small>{description}</small>}</div><button type="button" className="toggle" role="switch" aria-checked={checked} aria-label={label} disabled={disabled} onClick={() => onChange(!checked)}><span /></button></div>;
}
export function Markdown({ text }: { text: string }) {
  return <div className="markdown"><ReactMarkdown components={{ a: ({ href, children }) => <a href={href} target="_blank" rel="noopener noreferrer">{children}<ArrowUpRight size={11} /></a>, pre: ({ children }) => <pre tabIndex={0}>{children}</pre> }}>{text}</ReactMarkdown></div>;
}
function SavedImage({ image }: { image: string }) {
  const [src, setSrc] = useState(image.startsWith('forge-attachment:') ? '' : image.startsWith('data:') ? image : `data:image/jpeg;base64,${image}`);
  const [error, setError] = useState(''); const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!image.startsWith('forge-attachment:')) return;
    let disposed = false; let loaded = false;
    async function load() { if (loaded) return; loaded = true; try { const result = await api<{ image: string; mime_type?: string }>('attachment', { id: image }); if (!disposed) setSrc(`data:${result.mime_type || 'image/jpeg'};base64,${result.image}`); } catch { if (!disposed) setError('Saved image unavailable'); } }
    if (!('IntersectionObserver' in window)) { void load(); return () => { disposed = true; }; }
    const observer = new IntersectionObserver(entries => { if (entries.some(entry => entry.isIntersecting)) { observer.disconnect(); void load(); } }, { rootMargin: '150px' });
    if (ref.current) observer.observe(ref.current);
    return () => { disposed = true; observer.disconnect(); };
  }, [image]);
  return <div ref={ref} className="saved-image">{src ? <img alt="Attached image" loading="lazy" src={src} /> : <span>{error || 'Saved image'}</span>}</div>;
}
export function MessageView({ message, streaming = false, showThinking = true }: { message: Message; streaming?: boolean; showThinking?: boolean }) {
  const [copied, setCopied] = useState(false);
  if (message.role === 'tool') return <details className="tool-message"><summary><Wrench size={14} />{message.tool_name || 'Tool result'}</summary><pre>{message.content}</pre></details>;
  if (message.role === 'system') return null;
  async function copy() {
    try { if (isNative()) await native('copy', message.content); else await navigator.clipboard.writeText(message.content); setCopied(true); setTimeout(() => setCopied(false), 1500); } catch { /* Native clipboard permissions are reflected by browser UI. */ }
  }
  return <article className={`message ${message.role}`}>
    <div className="message-meta">{message.role === 'user' ? 'You' : <><img src="./forge.svg" alt="" />Forge</>}{message.status && message.status !== 'complete' && <span>{message.status}</span>}</div>
    {message.images?.length ? <div className="message-images">{message.images.map((image, i) => <SavedImage key={i} image={image} />)}</div> : null}
    {message.thinking && showThinking && <details className="reasoning" open={streaming || undefined}><summary>{streaming ? <LoaderCircle className="spin" size={14} /> : <Check size={14} />}{streaming ? 'Thinking' : 'Model reasoning'}</summary><div>{message.thinking}</div></details>}
    {streaming ? <div className="stream-text">{message.content}<span className="stream-caret" /></div> : <Markdown text={message.content} />}
    {!streaming && message.role !== 'user' && message.content && <button className="text-button copy-button" onClick={copy}>{copied ? <Check size={13} /> : <Copy size={13} />}{copied ? 'Copied' : 'Copy'}</button>}
    {message.sources?.length ? <details className="sources"><summary>Sources · {message.sources.length}</summary>{message.sources.map((s, i) => <a key={i} href={/^https?:\/\//.test(s.url) ? s.url : undefined} target="_blank" rel="noopener noreferrer">{s.title || s.url}</a>)}</details> : null}
  </article>;
}
export function ToolCard({ event }: { event: RunEvent }) { return <details className="tool-card"><summary>{event.state === 'running' ? <LoaderCircle size={13} className="spin" /> : event.state === 'done' ? <Check size={13} /> : <Info size={13} />}<span>{event.name?.replaceAll('_', ' ') || 'Tool action'}</span><small>{event.state}</small></summary><pre>{JSON.stringify(event.arguments ?? event.result ?? {}, null, 2)}</pre>{event.arguments && event.result !== undefined ? <pre>{typeof event.result === 'string' ? event.result : JSON.stringify(event.result, null, 2)}</pre> : null}</details>; }
export function ModelPicker({ models, value, select, open, setOpen, refresh, providerId = 'ollama' }: { models: Model[]; value: string; select: (name: string) => void; open: boolean; setOpen: (v: boolean) => void; refresh: () => void; providerId?: string }) {
  const [search, setSearch] = useState('');
  const [metadata, setMetadata] = useState<Record<string, Partial<Model>>>({});
  const inspected = useRef(new Set<string>());
  const ref = useRef<HTMLDivElement>(null);
  async function inspect(model: Model) {
    const key = `${providerId}:${model.name}`;
    if (inspected.current.has(key)) return; inspected.current.add(key);
    try { const result = await api<{ capabilities?: string[]; model_info?: Record<string, unknown> }>('show', { model: model.name, provider_id: providerId });
      const limit = Object.entries(result.model_info || {}).find(([key]) => key.endsWith('.context_length'))?.[1];
      setMetadata(prev => ({ ...prev, [key]: { capabilities: result.capabilities || [], context_length: typeof limit === 'number' ? limit : undefined } }));
    } catch { /* Unavailable capabilities stay unknown, never advertised as supported. */ }
  }
  useEffect(() => { if (open) { for (const model of models.filter(m => m.name.toLowerCase().includes(search.toLowerCase())).slice(0, 6)) void inspect(model); } }, [open, providerId, search, models]);
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => { if (!ref.current?.contains(e.target as Node)) setOpen(false); };
    const escape = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false); };
    document.addEventListener('mousedown', close); document.addEventListener('keydown', escape);
    return () => { document.removeEventListener('mousedown', close); document.removeEventListener('keydown', escape); };
  }, [open, setOpen]);
  return <div className="model-picker" ref={ref}><button className="model-trigger" aria-haspopup="dialog" aria-expanded={open} onClick={() => { setSearch(''); setOpen(!open); }} title={value || 'Select model'}><span>{value ? friendlyModel(value) : 'Select model'}</span><ChevronDown size={13} /></button>{open && <div className="model-popover" role="dialog" aria-label="Choose model"><div className="search-input"><Search size={16} /><input autoFocus placeholder="Search models…" aria-label="Search models" value={search} onChange={e => setSearch(e.target.value)} /></div><div className="model-list">{models.filter(m => m.name.toLowerCase().includes(search.toLowerCase())).map(item => { const model = { ...item, ...metadata[`${providerId}:${item.name}`] }; return <button className={`model-option ${model.name === value ? 'selected' : ''}`} key={model.name} onMouseEnter={() => void inspect(item)} onFocus={() => void inspect(item)} onClick={() => { select(model.name); setOpen(false); }}><div><strong>{friendlyModel(model.name)}</strong>{model.name === value && <Check size={15} />}</div><small>{model.name}</small><div className="model-tags">{model.capabilities?.includes('vision') && <span><Eye size={11} />Vision</span>}{model.capabilities?.includes('tools') && <span><Wrench size={11} />Tools</span>}{(model.context_length || model.max_context) && <span>{tokenLabel(model.context_length || model.max_context || 0)} context</span>}{model.size ? <span>{bytesLabel(model.size)}</span> : null}</div></button>; })}{!models.length && <div className="popover-empty">No models found. Connect an engine in Settings.</div>}</div><button className="popover-footer" onClick={refresh}>Refresh models</button></div>}</div>;
}
export const slashCommands = [
  ['builder', 'Explore ideas and shape a project together'], ['build', 'Accept the Builder brief and start building'], ['plan', 'Inspect and prepare a plan'], ['todo', 'Run the saved plan as a Markdown checklist'], ['goal', 'Work through a goal with checkpoints'], ['pause', 'Pause the active run'], ['resume', 'Continue a paused run'], ['status', 'Show run and goal progress'], ['compact', 'Compact context and keep your history'], ['new', 'Start a new conversation'], ['project', 'Choose a project'], ['model', 'Choose a local model'], ['agents', 'Configure or launch sub-agents'], ['worktree', 'Create an isolated Git workspace'], ['skill', 'Choose an installed skill'], ['mcp', 'Manage tool connections'], ['schedule', 'Schedule a task'], ['help', 'Show available commands'],
] as const;
export function SlashPalette({ query, select, activeIndex = 0 }: { query: string; select: (value: string) => void; activeIndex?: number }) {
  const search = query.slice(1).split(' ')[0].toLowerCase();
  return <div className="slash-palette" id="forge-slash-options" role="listbox" aria-label="Slash commands">{slashCommands.filter(([name]) => name.includes(search)).map(([name, description], index) => <button role="option" id={`forge-slash-${index}`} aria-selected={index === activeIndex} className={index === activeIndex ? 'selected' : ''} key={name} onMouseDown={e => e.preventDefault()} onClick={() => select(`/${name} `)}><strong>/{name}</strong><span>{description}</span></button>)}</div>;
}
export function useResource<T>(action: string, key: string, data: Record<string, unknown> = {}) {
  const [items, setItems] = useState<T[]>([]); const [loading, setLoading] = useState(true); const [error, setError] = useState('');
  const encoded = JSON.stringify(data);
  async function refresh() { setLoading(true); setError(''); try { const result = await api<Record<string, T[]> | T[]>(action, JSON.parse(encoded)); const list = Array.isArray(result) ? result : result[key] || []; if (Array.isArray(list)) setItems(list); else if (action === 'permission_overrides') setItems(Object.entries(list).map(([binding, profile]) => ({ scope: binding.split(':')[0], target: binding.slice(binding.indexOf(':') + 1), profile })) as T[]); else setItems([]); } catch (e) { setError(e instanceof Error ? e.message : String(e)); } finally { setLoading(false); } }
  useEffect(() => { void refresh(); }, [action, key, encoded]);
  return { items, setItems, loading, error, refresh };
}
