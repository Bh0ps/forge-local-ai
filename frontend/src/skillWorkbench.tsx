import { useEffect, useRef, useState } from 'react';
import { api, errorText } from './api';
import { Badge, ErrorNotice, Field, Markdown, Modal } from './components';
import type { LibrarySkill } from './pluginsPage';
import './library.css';

interface SkillDocument { skill: LibrarySkill & { revision?: string; manifest_error?: string }; text: string; revision: string; digest: string; manifest?: Record<string, unknown>; resources?: string[]; truncated?: boolean; }
interface SkillVersion { revision: string; saved_at: number; operation: string; version?: string; }
interface RoutedSkill { id: string; name: string; selection_reason: string; }

export function SkillWorkbench({ id, projectId, close, onSaved, notify }: { id: string; projectId?: string | null; close: () => void; onSaved: () => Promise<void>; notify: (message: string) => void }) {
  const [document, setDocument] = useState<SkillDocument | null>(null);
  const [text, setText] = useState(''); const [manifest, setManifest] = useState('');
  const [view, setView] = useState<'instructions' | 'edit' | 'routing' | 'versions'>('instructions');
  const [query, setQuery] = useState(''); const [phase, setPhase] = useState('');
  const [route, setRoute] = useState<{ selected: RoutedSkill[]; phase: string } | null>(null);
  const [versions, setVersions] = useState<SkillVersion[]>([]); const [resource, setResource] = useState('');
  const [error, setError] = useState(''); const [busy, setBusy] = useState(false); const generation = useRef(0);
  const scope = { id, project_id: projectId || undefined };
  async function load() {
    const request = ++generation.current; setError(''); setBusy(true);
    try { const result = await api<SkillDocument>('skill_preview', scope); if (request !== generation.current) return; setDocument(result); setText(result.text); setManifest(JSON.stringify(result.manifest || {}, null, 2)); setResource(''); }
    catch (e) { if (request === generation.current) setError(errorText(e)); }
    finally { if (request === generation.current) setBusy(false); }
  }
  useEffect(() => { void load(); return () => { generation.current++; }; }, [id, projectId]);
  async function mutate(action: string, data: Record<string, unknown>) {
    if (!document || busy) return; const request = generation.current; setBusy(true); setError('');
    try { const result = await api<SkillDocument>(action, { ...scope, expected_revision: document.revision, ...data }); if (request !== generation.current) return; setDocument(result); setText(result.text); setManifest(JSON.stringify(result.manifest || {}, null, 2)); setView('instructions'); await onSaved(); notify(action === 'skill_reset' ? 'Bundled guidance restored. Your previous version is saved.' : 'Skill saved. New guidance applies before the next model round.'); }
    catch (e) { if (request === generation.current) setError(errorText(e)); }
    finally { if (request === generation.current) setBusy(false); }
  }
  async function save() {
    let parsed: Record<string, unknown> | undefined;
    try { const value = JSON.parse(manifest); if (Object.keys(value).length) parsed = value; }
    catch { setError('Manifest must be valid JSON.'); return; }
    await mutate('skill_edit', { text, manifest: parsed || null });
  }
  async function testRoute() {
    const request = generation.current; setBusy(true); setError('');
    try { const result = await api<{ selected: RoutedSkill[]; phase: string }>('skill_route_preview', { query, project_id: projectId || undefined, ...(phase ? { phase } : {}) }); if (request === generation.current) setRoute(result); }
    catch (e) { if (request === generation.current) setError(errorText(e)); }
    finally { if (request === generation.current) setBusy(false); }
  }
  async function history() {
    setView('versions'); const request = generation.current; setBusy(true); setError('');
    try { const result = await api<{ versions: SkillVersion[] }>('skill_versions', scope); if (request === generation.current) setVersions(result.versions); }
    catch (e) { if (request === generation.current) setError(errorText(e)); }
    finally { if (request === generation.current) setBusy(false); }
  }
  async function readResource(path: string) {
    const request = generation.current; setBusy(true); setError('');
    try { const result = await api<{ text: string; truncated?: boolean }>('skills_resource_read', { ...scope, path, limit: 16000 }); if (request === generation.current) setResource(result.text + (result.truncated ? '\n\n[Resource excerpt. Request a later range through the resource tool.]' : '')); }
    catch (e) { if (request === generation.current) setError(errorText(e)); }
    finally { if (request === generation.current) setBusy(false); }
  }
  return <Modal title={`${document?.skill.name || 'Skill'} · Workbench`} wide close={close}>
    <div className="skill-workbench">
      <p className="muted">Edit reusable guidance, check when it activates and inspect saved versions. Your tool permissions continue to control every action.</p>
      <nav className="section-tabs" aria-label="Skill workbench views">{(['instructions', 'edit', 'routing', 'versions'] as const).map(item => <button key={item} aria-pressed={view === item} className={view === item ? 'active' : ''} disabled={busy} onClick={() => item === 'versions' ? void history() : setView(item)}>{item === 'instructions' ? 'Instructions' : item === 'edit' ? 'Edit' : item === 'routing' ? 'Test routing' : 'Versions'}</button>)}</nav>
      {error && <ErrorNotice error={error} retry={() => void load()} />}
      {busy && <p role="status">Working…</p>}
      {document?.skill.manifest_error && <ErrorNotice error={`Manifest needs attention: ${document.skill.manifest_error}`} />}
      {view === 'instructions' && document && <><div className="skill-workbench-meta"><Badge>{document.skill.scope || 'global'}</Badge><Badge>{document.skill.enabled ? 'Enabled' : 'Disabled'}</Badge><code title={document.revision}>Revision {document.revision?.slice(0, 12)}</code></div><Markdown text={document.text.replace(/^---\r?\n[\s\S]*?\r?\n---(?:\r?\n|$)/, '')} />{document.truncated && <p className="muted">Preview is an excerpt; editing is disabled to preserve the full file.</p>}{Boolean(document.resources?.length) && <div className="skill-resource-list"><h3>Reference resources</h3>{document.resources!.map(path => <button className="secondary" disabled={busy || !document.skill.enabled} key={path} onClick={() => void readResource(path)}>{path}</button>)}</div>}{resource && <div className="skill-resource-preview"><Markdown text={resource} /></div>}</>}
      {view === 'edit' && document && <><Field label="Skill instructions" hint="Portable SKILL.md content including its frontmatter. Saving retains the previous version."><textarea aria-label="Skill instructions" rows={16} value={text} disabled={Boolean(document.truncated)} onChange={e => setText(e.target.value)} /></Field><details><summary>Forge routing manifest</summary><p className="muted">Optional schema_version 1 metadata for phases, intents, triggers, resources and required tools. It cannot grant access.</p><textarea aria-label="Skill routing manifest" rows={10} value={manifest} onChange={e => setManifest(e.target.value)} /></details><div className="skill-workbench-actions"><button className="secondary" disabled={busy} onClick={() => void load()}>Reload file</button><button className="primary" disabled={busy || Boolean(document.truncated) || !text.trim()} onClick={() => void save()}>Save skill</button>{document.skill.builtin && <button className="text-button" disabled={busy} onClick={() => void mutate('skill_reset', {})}>Restore bundled version</button>}</div></>}
      {view === 'routing' && <><Field label="Example task"><textarea aria-label="Example task" rows={3} value={query} onChange={e => setQuery(e.target.value)} placeholder="Add a responsive account settings page…" /></Field><Field label="Task phase"><select value={phase} onChange={e => setPhase(e.target.value)}><option value="">Infer from task</option>{['discover','plan','implement','verify','research'].map(item => <option key={item}>{item}</option>)}</select></Field><button className="primary" disabled={busy || !query.trim()} onClick={() => void testRoute()}>Preview skill selection</button>{route && <div className="skill-routing-results" aria-live="polite"><p>Phase: {route.phase} · {route.selected.length} selected</p>{!route.selected.length && <p>No enabled automatic skills match this task.</p>}{route.selected.map(item => <article key={item.id}><strong>{item.name}</strong><p>{item.selection_reason}</p>{item.id === id && <Badge tone="accent">This skill</Badge>}</article>)}</div>}</>}
      {view === 'versions' && <div className="skill-version-list">{!versions.length && !busy && <p className="muted">Saved versions appear after your first edit.</p>}{versions.map((item, index) => <article key={`${item.revision}-${index}`}><div><strong>{item.operation}</strong><small>{new Date(item.saved_at * 1000).toLocaleString()} · {item.revision.slice(0, 12)}</small></div><button className="secondary" disabled={busy || item.revision === document?.revision} onClick={() => void mutate('skill_restore', { revision: item.revision })}>Restore</button></article>)}</div>}
    </div><footer><button className="subtle" onClick={close}>Close workbench</button></footer>
  </Modal>;
}

export function SkillSelector({ value, onChange, projectId, label = 'Skills' }: { value: string[]; onChange: (value: string[]) => void; projectId?: string | null; label?: string }) {
  const [skills, setSkills] = useState<LibrarySkill[]>([]); const [query, setQuery] = useState(''); const [error, setError] = useState('');
  useEffect(() => { let disposed = false; void api<{ skills: LibrarySkill[] }>('skills', { project_id: projectId || undefined }).then(result => { if (!disposed) { setSkills(result.skills); setError(''); } }).catch(e => { if (!disposed) setError(errorText(e)); }); return () => { disposed = true; }; }, [projectId]);
  const selected = (skill: LibrarySkill) => value.some(item => [skill.id, skill.name, skill.library_id].includes(item));
  const visible = skills.filter(skill => `${skill.name} ${skill.description || ''}`.toLocaleLowerCase().includes(query.toLocaleLowerCase()));
  const unresolved = value.filter(item => !skills.some(skill => [skill.id, skill.name, skill.library_id].includes(item)));
  return <div className="skill-selector"><label>{label}</label><input aria-label={`Search ${label.toLowerCase()}`} value={query} onChange={e => setQuery(e.target.value)} placeholder="Search enabled workflows…" />{error && <p role="alert">{error}</p>}<div className="skill-selector-chips">{value.map(item => <button type="button" className="secondary" key={item} onClick={() => onChange(value.filter(name => name !== item))}>{skills.find(skill => [skill.id, skill.name, skill.library_id].includes(item))?.name || item} ×</button>)}</div>{Boolean(unresolved.length) && <p className="muted">Unavailable selections: {unresolved.join(', ')}</p>}<div className="skill-selector-list">{visible.map(skill => <label key={skill.id}><input type="checkbox" disabled={!skill.enabled} checked={selected(skill)} onChange={e => onChange(e.target.checked ? [...value, skill.id] : value.filter(item => ![skill.id, skill.name, skill.library_id].includes(item)))} /><span>{skill.name}<small>{skill.enabled ? skill.description : 'Disabled in Library'}</small></span></label>)}</div></div>;
}
