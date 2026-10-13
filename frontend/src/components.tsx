import { cloneElement, isValidElement, useEffect, useId, useRef, useState } from 'react';
import { Brain, Check, ChevronDown, Copy, Info, Search, X, Eye, Wrench, LoaderCircle, ArrowUpRight } from 'lucide-react';
import ReactMarkdown, { type Components } from 'react-markdown';
import { api, bytesLabel, friendlyModel, isNative, native, tokenLabel } from './api';
import type { Message, Model, RunEvent } from './types';
import { ThinkingDisclosure } from './thinkingDisclosure';
import { useDialogReadiness } from './updateReadiness';
import { composerModes } from './composerModes';
import type { DraftCommandMode } from './persistentDraft';
import './composerModes.css';

export function IconButton({ label, children, onClick, disabled, active, className = '' }: { label: string; children: React.ReactNode; onClick?: () => void; disabled?: boolean; active?: boolean; className?: string }) {
  return <button type="button" className={`icon-button ${active ? 'selected' : ''} ${className}`} aria-label={label} title={label} aria-pressed={active} onClick={onClick} disabled={disabled}>{children}</button>;
}
export function Modal({ title, children, close, wide = false }: { title: string; children: React.ReactNode; close: () => void; wide?: boolean }) {
  useDialogReadiness();
  const ref = useRef<HTMLDialogElement>(null), titleId = useId();
  useEffect(() => { const trigger = document.activeElement as HTMLElement | null; ref.current?.showModal(); return () => {ref.current?.close(); if (trigger?.isConnected) trigger.focus();}; }, []);
  return <dialog ref={ref} aria-labelledby={titleId} className={`modal ${wide ? 'wide' : ''}`} onCancel={close} onClick={e => { if (e.target === e.currentTarget) close(); }}><header><h2 id={titleId}>{title}</h2><IconButton label="Close dialog" onClick={close}><X size={18} /></IconButton></header>{children}</dialog>;
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
function CodeBlock({children}: {children?:React.ReactNode}) {
  const code=isValidElement<{children?:React.ReactNode;className?:string}>(children)?children.props:null;
  const text=typeof code?.children==='string'?code.children:'';
  const language=code?.className?.replace(/^language-/,'') || 'Code';
  const [state,setState]=useState('Copy'), timer=useRef<ReturnType<typeof setTimeout>|null>(null);
  useEffect(()=>()=>{if(timer.current)clearTimeout(timer.current);},[]);
  async function copy(){try{if(isNative())await native('copy',text);else await navigator.clipboard.writeText(text);setState('Copied');}catch{setState('Copy failed');}if(timer.current)clearTimeout(timer.current);timer.current=setTimeout(()=>setState('Copy'),2000);}
  return <div className="code-block"><header><span>{language}</span><button type="button" aria-label="Copy code" onClick={()=>void copy()}><Copy size={13}/><span role="status">{state}</span></button></header><pre tabIndex={0}>{children}</pre></div>;
}
const markdownComponents:Components={a:({href,children})=><a href={href} target="_blank" rel="noopener noreferrer">{children}<ArrowUpRight size={11}/></a>,pre:CodeBlock};
export function Markdown({ text }: { text: string }) {
  return <div className="markdown"><ReactMarkdown components={markdownComponents}>{text}</ReactMarkdown></div>;
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
  const [copied, setCopied] = useState(false), [copyError,setCopyError]=useState(false);
  if (message.role === 'tool') return <details className="tool-message"><summary><Wrench size={14} />{message.tool_name || 'Tool result'}</summary><pre>{message.content}</pre></details>;
  if (message.role === 'system') return null;
  async function copy() {
    try { if (isNative()) await native('copy', message.content); else await navigator.clipboard.writeText(message.content); setCopyError(false);setCopied(true); setTimeout(() => setCopied(false), 1500); } catch {setCopyError(true);}
  }
  return <article className={`message ${message.role}`}>
    <div className="message-meta">{message.role === 'user' ? 'You' : <><img src="./forge.svg" alt="" />Forge</>}{message.status && message.status !== 'complete' && <span>{message.status}</span>}</div>
    {message.images?.length ? <div className="message-images">{message.images.map((image, i) => <SavedImage key={i} image={image} />)}</div> : null}
    {message.thinking && showThinking && <ThinkingDisclosure key={message.id ?? message.run_id} text={message.thinking} streaming={streaming} active={streaming && !message.content} />}
    <Markdown text={message.content}/>{streaming && <span className="stream-caret" aria-hidden="true"/>}
    {!streaming && message.role !== 'user' && message.content && <button className="text-button copy-button" onClick={copy}>{copied ? <Check size={13} /> : <Copy size={13} />}{copied ? 'Copied' : 'Copy'}</button>}
    {copyError && <small role="alert">Could not copy. Select the text to copy it manually.</small>}
    {message.sources?.length ? <details className="sources"><summary>Sources · {message.sources.length}</summary>{message.sources.map((s, i) => <a key={i} href={/^https?:\/\//.test(s.url) ? s.url : undefined} target="_blank" rel="noopener noreferrer">{s.title || s.url}</a>)}</details> : null}
  </article>;
}
export function ToolCard({ event }: { event: RunEvent }) { return <details className="tool-card"><summary>{event.state === 'running' ? <LoaderCircle size={13} className="spin" /> : event.state === 'done' ? <Check size={13} /> : <Info size={13} />}<span>{event.name?.replaceAll('_', ' ') || 'Tool action'}</span><small>{event.state}</small></summary><pre>{JSON.stringify(event.arguments ?? event.result ?? {}, null, 2)}</pre>{event.arguments && event.result !== undefined ? <pre>{typeof event.result === 'string' ? event.result : JSON.stringify(event.result, null, 2)}</pre> : null}</details>; }
export function reasoningCapability(model:Model,providerId:string):string|null {
  if((providerId==='openrouter' || model.provider==='openrouter') && ['openrouter/free','openrouter/auto'].includes(model.name))return 'Reasoning varies by route';
  if(!Array.isArray(model.capabilities))return null;
  return model.capabilities.includes('thinking')?'Reasoning':'No reasoning control';
}
export function ModelPicker({ models, value, select, open, setOpen, refresh, providerId = 'ollama' }: { models: Model[]; value: string; select: (name: string) => void; open: boolean; setOpen: (v: boolean) => void; refresh: () => void; providerId?: string }) {
  const [search, setSearch] = useState('');
  const [metadata, setMetadata] = useState<Record<string, Partial<Model>>>({});
  const inspected = useRef(new Set<string>());
  const pendingMetadata=useRef(new Set<string>()), metadataGeneration=useRef(0), trigger=useRef<HTMLButtonElement>(null);
  useEffect(()=>()=>{metadataGeneration.current++;pendingMetadata.current.clear();},[]);
  const ref = useRef<HTMLDivElement>(null);
  async function inspect(model: Model) {
    const key = `${providerId}:${model.name}`;
    if (inspected.current.has(key) || pendingMetadata.current.has(key)) return; pendingMetadata.current.add(key);const generation=metadataGeneration.current;
    try { const result = await api<{ capabilities?: string[]; model_info?: Record<string, unknown> }>('show', { model: model.name, provider_id: providerId });
      const limit = Object.entries(result.model_info || {}).find(([key]) => key.endsWith('.context_length'))?.[1];
      if(generation!==metadataGeneration.current)return;inspected.current.add(key);
      setMetadata(prev => ({ ...prev, [key]: { ...(Array.isArray(result.capabilities)?{capabilities:result.capabilities}:{}), ...(typeof limit==='number'?{context_length:limit}:{}) } }));
    } catch { /* Failed probes remain retryable, without advertising support. */ }finally{if(generation===metadataGeneration.current)pendingMetadata.current.delete(key);}
  }
  useEffect(() => { if (open) { for (const model of models.filter(m => m.name.toLowerCase().includes(search.toLowerCase())).slice(0, 6)) void inspect(model); } }, [open, providerId, search, models]);
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => { if (!ref.current?.contains(e.target as Node)) setOpen(false); };
    const escape = (e: KeyboardEvent) => { if (e.key === 'Escape') {setOpen(false);trigger.current?.focus();} };
    document.addEventListener('mousedown', close); document.addEventListener('keydown', escape);
    return () => { document.removeEventListener('mousedown', close); document.removeEventListener('keydown', escape); };
  }, [open, setOpen]);
  return <div className="model-picker" ref={ref}><button ref={trigger} className="model-trigger" aria-haspopup="dialog" aria-expanded={open} onClick={() => { setSearch(''); setOpen(!open); }} title={value || 'Select model'}><span>{value ? friendlyModel(value) : 'Select model'}</span><ChevronDown size={13} /></button>{open && <div className="model-popover" role="dialog" aria-label="Choose model"><div className="search-input"><Search size={16} /><input autoFocus placeholder="Search models…" aria-label="Search models" value={search} onChange={e => setSearch(e.target.value)} /></div><div className="model-list">{models.filter(m => m.name.toLowerCase().includes(search.toLowerCase())).map(item => { const model = { ...item, ...metadata[`${providerId}:${item.name}`] },reasoning=reasoningCapability(model,providerId); return <button className={`model-option ${model.name === value ? 'selected' : ''}`} key={model.name} onMouseEnter={() => void inspect(item)} onFocus={() => void inspect(item)} onClick={() => { select(model.name); setOpen(false);trigger.current?.focus(); }}><div><strong>{friendlyModel(model.name)}</strong>{model.name === value && <Check size={15} />}</div><small>{model.name}</small><div className="model-tags">{model.capabilities?.includes('vision') && <span><Eye size={11} />Vision</span>}{model.capabilities?.includes('tools') && <span><Wrench size={11} />Tools</span>}{reasoning && <span><Brain size={11}/>{reasoning}</span>}{(model.context_length || model.max_context) && <span>{tokenLabel(model.context_length || model.max_context || 0)} context</span>}{model.size ? <span>{bytesLabel(model.size)}</span> : null}</div></button>; })}{!models.length && <div className="popover-empty">No models found. Connect an engine in Settings.</div>}</div><button className="popover-footer" onClick={()=>{metadataGeneration.current++;inspected.current.clear();pendingMetadata.current.clear();setMetadata({});for(const model of models.filter(item=>item.name.toLowerCase().includes(search.toLowerCase())).slice(0,6))void inspect(model);refresh();}}>Refresh models</button></div>}</div>;
}
export const slashCommands = [
  ['builder', 'Explore ideas and shape a project together'], ['build', 'Accept the Builder brief and start building'], ['plan', 'Inspect and prepare a plan'], ['todo', 'Run the saved plan as a Markdown checklist'], ['goal', 'Work through a goal with checkpoints'], ['do', 'Run a task with tools'], ['pause', 'Pause the active run'], ['resume', 'Continue a paused run'], ['status', 'Show run and goal progress'], ['compact', 'Compact context and keep your history'], ['new', 'Start a new conversation'], ['project', 'Choose a project'], ['model', 'Choose a local model'], ['agents', 'Configure or launch sub-agents'], ['worktree', 'Create an isolated Git workspace'], ['skill', 'Choose an installed skill'], ['mcp', 'Manage tool connections'], ['schedule', 'Schedule a task'], ['help', 'Show available commands'],
] as const;
export function ComposerModeChip({mode,remove}:{mode:DraftCommandMode;remove:()=>void}) {
  const info=composerModes[mode];
  return <div className="composer-mode-row"><span className="composer-mode-chip"><Wrench size={12} aria-hidden="true"/><span>Next request: {info.label}</span><button type="button" aria-label={`Remove ${info.label} mode`} title="Return to normal chat" onClick={remove}><X size={12} aria-hidden="true"/></button></span><small>{info.description}</small></div>;
}
export function SlashPalette({ query, select, activeIndex = 0 }: { query: string; select: (value: string) => void; activeIndex?: number }) {
  const search = query.slice(1).split(' ')[0].toLowerCase();
  return <div className="slash-palette" id="forge-slash-options" role="listbox" aria-label="Slash commands">{slashCommands.filter(([name]) => name.includes(search)).map(([name, description], index) => <button role="option" aria-label={`/${name} · ${description}`} id={`forge-slash-${index}`} aria-selected={index === activeIndex} className={index === activeIndex ? 'selected' : ''} key={name} onMouseDown={e => e.preventDefault()} onClick={() => select(`/${name} `)}><strong>/{name}</strong><span>{description}</span></button>)}</div>;
}
export function useResource<T>(action: string, key: string, data: Record<string, unknown> = {}) {
  const [items, setItems] = useState<T[]>([]); const [loading, setLoading] = useState(true); const [error, setError] = useState('');
  const encoded = JSON.stringify(data);
  const generation=useRef(0);
  async function refresh() {const request=++generation.current;setLoading(true);setError('');try{const result=await api<Record<string,T[]>|T[]>(action,JSON.parse(encoded));if(request!==generation.current)return;const list=Array.isArray(result)?result:result[key]||[];if(Array.isArray(list))setItems(list);else if(action==='permission_overrides')setItems(Object.entries(list).map(([binding,profile])=>({scope:binding.split(':')[0],target:binding.slice(binding.indexOf(':')+1),profile})) as T[]);else setItems([]);}catch(e){if(request===generation.current)setError(e instanceof Error?e.message:String(e));}finally{if(request===generation.current)setLoading(false);}}
  useEffect(() => {setItems([]);void refresh();return()=>{generation.current++;};}, [action, key, encoded]);
  return { items, setItems, loading, error, refresh };
}
