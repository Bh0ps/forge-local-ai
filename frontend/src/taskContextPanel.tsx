import { useEffect, useRef, useState } from 'react';
import { BookOpen, Search, Sparkles, X } from 'lucide-react';
import { api, errorText } from './api';
import { ErrorNotice } from './components';
import type { LibrarySkill } from './pluginsPage';

type Note = {id: string; name: string; project_ids?: string[]};
type Props = {projectId?: string | null; skills: string[]; notes: string[]; spaces: Note[]; onSkills: (ids: string[]) => void; onNotes: (ids: string[]) => void};

export function TaskContextPanel({projectId, skills, notes, spaces, onSkills, onNotes}: Props) {
  const [catalog, setCatalog] = useState<LibrarySkill[]>([]), [query, setQuery] = useState('');
  const [error, setError] = useState(''), [loading, setLoading] = useState(true), [retry, setRetry] = useState(0);
  const search = useRef<HTMLInputElement>(null);
  useEffect(() => {
    let current = true; setLoading(true); setError(''); setCatalog([]);
    void api<{skills: LibrarySkill[]}>('skills', {project_id: projectId || undefined}).then(result => {
      if (current) setCatalog(result.skills || []);
    }).catch(failure => {if (current) setError(errorText(failure));}).finally(() => {if (current) setLoading(false);});
    return () => {current = false;};
  }, [projectId, retry]);
  const availableNotes = spaces.filter(note => !note.project_ids?.length || Boolean(projectId && note.project_ids.includes(projectId)));
  const matches = (name: string, description = '') => `${name} ${description}`.toLowerCase().includes(query.trim().toLowerCase());
  const toggle = (values: string[], id: string) => values.includes(id) ? values.filter(value => value !== id) : [...values, id];
  const selected = [...[...new Set(skills)].map(id => {const name=catalog.find(skill => skill.id === id || skill.library_id === id)?.name;return {key:`skill:${id}`, name:name || (loading?'Saved skill':'Unavailable skill'), unavailable:!name && !loading, remove:() => onSkills(skills.filter(value => value !== id))};}), ...[...new Set(notes)].map(id => {const name=spaces.find(note => note.id === id)?.name;return {key:`note:${id}`,name:name || 'Unavailable note',unavailable:!name,remove:() => onNotes(notes.filter(value => value !== id))};})];
  const shownSkills = catalog.filter(skill => matches(skill.name, skill.description)), shownNotes = availableNotes.filter(note => matches(note.name));
  return <section className="task-context-panel" aria-label="Skills and reference notes">
    <div className="context-attachment-summary"><strong>{selected.length ? `${selected.length} attached` : 'Nothing attached yet'}</strong></div>
    <div className="context-selected">{selected.map(item => <span className={`context-chip${item.unavailable?' unavailable':''}`} key={item.key}><span className="context-chip-name">{item.name}</span><button type="button" aria-label={`Remove ${item.name}`} onClick={item.remove}><X size={12}/></button></span>)}{!selected.length && <small>Forge can select relevant skills automatically.</small>}</div>
    <label className="context-search"><Search size={15}/><input ref={search} aria-label="Search skills and notes" autoFocus placeholder="Search skills and notes…" value={query} onChange={event => setQuery(event.target.value)}/>{query && <button type="button" aria-label="Clear context search" title="Clear search" onClick={()=>{setQuery('');search.current?.focus();}}><X size={13}/></button>}</label>
    {error && <ErrorNotice error={error} retry={() => setRetry(value => value + 1)}/>}
    {loading && <p role="status">Loading skills…</p>}
    <h3><Sparkles size={14}/> Skills</h3>
    {shownSkills.map(skill => <label className="context-choice" key={skill.id}><input type="checkbox" checked={skills.includes(skill.id) || Boolean(skill.library_id && skills.includes(skill.library_id))} disabled={!skill.enabled} onChange={() => {
      const id = skill.library_id && skills.includes(skill.library_id) ? skill.library_id : skill.id; onSkills(toggle(skills, id));
    }}/><span><strong>{skill.name}</strong><small>{skill.description}{!skill.enabled && ' · Disabled in Library'}</small></span></label>)}
    {!loading && !error && !shownSkills.length && <p className="context-empty">{query.trim()?'No skills match this search.':'No skills available for this project. Add them in Library.'}</p>}
    <h3><BookOpen size={14}/> Reference notes</h3>
    {shownNotes.map(note => <label className="context-choice" key={note.id}><input type="checkbox" checked={notes.includes(note.id)} disabled={!notes.includes(note.id) && notes.length >= 5} onChange={() => onNotes(toggle(notes, note.id))}/><span><strong>{note.name}</strong><small>Attach this Space to your message</small></span></label>)}
    {!shownNotes.length && <p className="context-empty">{query.trim() && availableNotes.length?'No notes match this search.':'Add notes in Spaces to use them here.'}</p>}
    {notes.length >= 5 && <p className="context-note-limit">Five notes attached. Remove one to choose another.</p>}
  </section>;
}
