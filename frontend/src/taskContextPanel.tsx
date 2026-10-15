import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react';
import { BookOpen, ChevronDown, Code2, Database, Globe, Layers3, Package, Palette, Search, ShieldCheck, Sparkles, X } from 'lucide-react';
import { api, errorText } from './api';
import { ErrorNotice, useControlLabel } from './components';
import type { LibrarySkill } from './pluginsPage';
import './taskContextPanel.css';

type Note = {id: string; name: string; description?: string; project_ids?: string[]};
type Props = {projectId?: string | null; skills: string[]; notes: string[]; spaces: Note[]; onSkills: (ids: string[]) => void; onNotes: (ids: string[]) => void};

const BUNDLES: Record<string, {name: string; icon: typeof Code2; tone: string}> = {
  build: {name: 'Coding', icon: Code2, tone: 'blue'}, coding: {name: 'Coding', icon: Code2, tone: 'blue'}, development: {name: 'Coding', icon: Code2, tone: 'blue'},
  design: {name: 'Design', icon: Palette, tone: 'violet'}, backend: {name: 'Backend', icon: Database, tone: 'green'},
  review: {name: 'Review', icon: ShieldCheck, tone: 'green'}, research: {name: 'Research', icon: Globe, tone: 'blue'},
  delivery: {name: 'Delivery', icon: Package, tone: 'amber'}, workflow: {name: 'Workflow', icon: Layers3, tone: 'violet'},
};

function skillBundle(skill: LibrarySkill) {
  const source = skill.bundle?.trim() || skill.category?.trim() || '';
  const known = BUNDLES[source.toLocaleLowerCase()];
  const name = known?.name || (source ? source.replaceAll(/[_-]/g, ' ').replace(/^\w/, value => value.toUpperCase()) : 'Your skills');
  return {key: name.toLocaleLowerCase(), name, icon: known?.icon || Sparkles, tone: known?.tone || 'violet', ungrouped: !source};
}

function ContextAction({label, expanded, controls, children, click}: {label: string; expanded?: boolean; controls?: string; children: React.ReactNode; click: (button: HTMLButtonElement) => void}) {
  const hint = useControlLabel(label);
  return <button type="button" className="context-row-action" aria-label={label} aria-expanded={expanded} aria-controls={controls} {...hint.events} onClick={event => {hint.dismiss();click(event.currentTarget);}}>{children}{hint.content}</button>;
}

function ContextChoice({identity, name, description, checked, disabled, status, change, remove, kind = 'skill'}: {
  identity: string; name: string; description?: string; checked: boolean; disabled?: boolean; status?: string;
  change: (origin: HTMLInputElement) => void; remove?: (origin: HTMLButtonElement) => void; kind?: 'skill' | 'note';
}) {
  const id = useId(), [expanded, setExpanded] = useState(false);
  return <div className={`context-item context-item-${kind}${disabled ? ' context-item-disabled' : ''}`}>
    <label className="context-choice"><input type="checkbox" data-context-identity={identity} aria-label={name} checked={checked} disabled={disabled} onChange={event => change(event.currentTarget)}/><span><strong>{name}</strong>{status && <small>{status}</small>}</span></label>
    <div className="context-item-actions">
      {description && <ContextAction label={`About ${name}`} expanded={expanded} controls={id} click={() => setExpanded(value => !value)}><ChevronDown size={14} aria-hidden="true"/></ContextAction>}
      {remove && <ContextAction label={`Remove ${name}`} click={remove}><X size={13} aria-hidden="true"/></ContextAction>}
    </div>
    {expanded && description && <p id={id} className="context-item-description">{description}</p>}
  </div>;
}

export function TaskContextPanel({projectId, skills, notes, spaces, onSkills, onNotes}: Props) {
  const [catalog, setCatalog] = useState<LibrarySkill[]>([]), [query, setQuery] = useState('');
  const [error, setError] = useState(''), [loading, setLoading] = useState(true), [retry, setRetry] = useState(0);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const search = useRef<HTMLInputElement>(null), groupId = useId();
  const root = useRef<HTMLElement>(null), pendingFocus = useRef<{origin: HTMLElement; identity?: string; projectId: Props['projectId']; query: string; transport: unknown} | null>(null);
  function change(origin: HTMLElement, operation: () => void, identity?: string) {
    if (document.activeElement === origin) pendingFocus.current = {origin, identity, projectId, query, transport: window.pywebview?.api.call || window.fetch};
    operation();
  }
  useLayoutEffect(() => {
    const request = pendingFocus.current; pendingFocus.current = null;
    if (!request || request.projectId !== projectId || request.query !== query || request.transport !== (window.pywebview?.api.call || window.fetch)
      || document.visibilityState === 'hidden' || !document.hasFocus() || !root.current?.isConnected) return;
    if (document.activeElement !== request.origin && (document.activeElement !== document.body || request.origin.isConnected)) return;
    const replacement = request.identity && [...root.current.querySelectorAll<HTMLInputElement>('input[data-context-identity]')].find(input => input.dataset.contextIdentity === request.identity && !input.disabled);
    (replacement || search.current)?.focus({preventScroll: true});
  }, [skills, notes, catalog, query, projectId]);
  useEffect(() => {
    let current = true; setLoading(true); setError(''); setCatalog([]);
    void api<{skills: LibrarySkill[]}>('skills', {project_id: projectId || undefined}).then(result => {
      if (current) setCatalog(result.skills || []);
    }).catch(failure => {if (current) setError(errorText(failure));}).finally(() => {if (current) setLoading(false);});
    return () => {current = false;};
  }, [projectId, retry]);
  const terms = query.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
  const matches = (...values: (string | undefined)[]) => terms.every(term => values.filter(Boolean).join(' ').toLocaleLowerCase().includes(term));
  const selectedSkill = (skill: LibrarySkill) => skills.includes(skill.id) || Boolean(skill.library_id && skills.includes(skill.library_id));
  const toggle = (values: string[], id: string) => values.includes(id) ? values.filter(value => value !== id) : [...values, id];
  const selectedSkills = new Map<string, {ids: string[]; skill?: LibrarySkill}>();
  for (const id of new Set(skills)) {
    const skill = catalog.find(item => item.id === id || item.library_id === id), key = skill?.id ?? id;
    const selection = selectedSkills.get(key);
    if (selection) selection.ids.push(id); else selectedSkills.set(key, {ids: [id], skill});
  }
  const selectedNotes = [...new Set(notes)], attachedCount = selectedSkills.size + selectedNotes.length;
  const availableNotes = spaces.filter(note => !note.project_ids?.length || Boolean(projectId && note.project_ids.includes(projectId)));
  const shownSkills = catalog.filter(skill => matches(skill.name, skill.description, skill.bundle, skill.category, skillBundle(skill).name, ...(skill.tags || [])));
  const shownNotes = availableNotes.filter(note => matches(note.name, note.description));
  const bundles = new Map<string, ReturnType<typeof skillBundle> & {skills: LibrarySkill[]}>();
  for (const skill of shownSkills.filter(skill => !selectedSkill(skill))) {
    const bundle = skillBundle(skill), group = bundles.get(bundle.key);
    if (group) group.skills.push(skill); else bundles.set(bundle.key, {...bundle, skills: [skill]});
  }
  return <section ref={root} className="task-context-panel" aria-label="Skills and reference notes">
    {attachedCount > 0 && <section className="context-attached" aria-label="Attached skills and notes"><div className="context-attachment-summary"><strong>{attachedCount} attached</strong></div><div className="context-attached-items">
      {[...selectedSkills.entries()].map(([key, {ids, skill}]) => {
        const name = skill?.name || (loading ? 'Saved skill' : 'Unavailable skill');
        const remove = () => onSkills(skills.filter(value => !ids.includes(value)));
        return <ContextChoice key={`skill:${key}`} identity={`skill:${key}`} name={name} description={skill?.description} checked disabled={!skill?.enabled} status={skill && !skill.enabled ? 'Disabled in Library' : undefined} change={origin => change(origin, remove, `skill:${key}`)} remove={origin => change(origin, remove)}/>;
      })}
      {selectedNotes.map(id => {
        const note = spaces.find(item => item.id === id), name = note?.name || 'Unavailable note';
        return <ContextChoice key={`note:${id}`} identity={`note:${id}`} kind="note" name={name} description={note?.description} checked disabled={!note} status={note && !availableNotes.includes(note) ? 'Outside this project' : undefined} change={origin => change(origin, () => onNotes(notes.filter(value => value !== id)), `note:${id}`)} remove={origin => change(origin, () => onNotes(notes.filter(value => value !== id)))}/>;
      })}
    </div></section>}
    <label className="context-search"><Search size={15} aria-hidden="true"/><input ref={search} aria-label="Search skills and notes" autoFocus placeholder="Search skills and notes" value={query} onChange={event => setQuery(event.target.value)}/>{query && <button type="button" aria-label="Clear context search" title="Clear search" onClick={() => {setQuery('');search.current?.focus();}}><X size={13} aria-hidden="true"/></button>}</label>
    {error && <ErrorNotice error={error} retry={() => setRetry(value => value + 1)}/>}
    {loading && <p className="context-empty" role="status">Loading skills…</p>}
    {(bundles.size > 0 || !shownSkills.length || loading || error) && <section className="context-skills" aria-label="Available skills"><h3><Sparkles size={14} aria-hidden="true"/>Skills</h3>
      {[...bundles.values()].map((bundle, index) => {
        const key = `${projectId || ''}:${query.trim().toLocaleLowerCase()}:${bundle.key}`;
        const open = expanded[key] ?? (terms.length > 0 || bundle.ungrouped);
        const id = `${groupId}-${index}`, Icon = bundle.icon;
        return <div className={`context-bundle context-tone-${bundle.tone}`} key={bundle.key}>
          <button type="button" className="context-bundle-toggle" aria-label={`${bundle.name} skills`} aria-expanded={open} aria-controls={id} onClick={() => setExpanded(value => ({...value, [key]: !open}))}><Icon size={16} aria-hidden="true"/><strong>{bundle.name}</strong><span>{bundle.skills.length}</span><ChevronDown size={14} aria-hidden="true"/></button>
          {open && <div id={id} className="context-bundle-items">{bundle.skills.map(skill => <ContextChoice key={skill.id} identity={`skill:${skill.id}`} name={skill.name} description={skill.description} checked={false} disabled={!skill.enabled} status={!skill.enabled ? 'Disabled in Library' : undefined} change={origin => change(origin, () => onSkills(toggle(skills, skill.id)), `skill:${skill.id}`)}/>)}</div>}
        </div>;
      })}
      {!loading && !error && !shownSkills.length && <p className="context-empty">{terms.length ? 'No skills match this search.' : 'No skills available.'}</p>}
    </section>}
    {(shownNotes.some(note => !notes.includes(note.id)) || !shownNotes.length) && <section className="context-notes" aria-label="Available notes"><h3><BookOpen size={14} aria-hidden="true"/>Notes</h3>
      {shownNotes.filter(note => !notes.includes(note.id)).map(note => <ContextChoice key={note.id} identity={`note:${note.id}`} kind="note" name={note.name} description={note.description} checked={false} disabled={selectedNotes.length >= 5} change={origin => change(origin, () => onNotes(toggle(notes, note.id)), `note:${note.id}`)}/>) }
      {!shownNotes.length && <p className="context-empty">{terms.length && availableNotes.length ? 'No notes match this search.' : 'No notes available.'}</p>}
      {selectedNotes.length >= 5 && <p className="context-note-limit">Five-note limit. Remove one to attach another.</p>}
    </section>}
  </section>;
}
