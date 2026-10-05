import { useEffect, useState } from 'react';
import { Brain, Check, FileText, Plus, RefreshCw, Search, Trash2, X } from 'lucide-react';
import { api, errorText } from './api';
import { Badge, Empty, ErrorNotice, Field, IconButton, Modal, PageHeader, Toggle } from './components';
import type { Agent, Project, Settings } from './types';
import './memoryPage.css';

interface Memory { id: string; title: string; content: string; kind: string; scope: string; status: string; revision: number; project_id?: string; source?: Record<string, unknown>; }
interface Skill { id: string; name: string; description: string; markdown: string; scope: string; status: string; revision: number; license: string; source?: { note?: string }; }
export function MemoryPage({ projects, settings, onChange, notify }: { projects: Project[]; settings: Settings; onChange: (v: Partial<Settings>) => Promise<void>; notify: (text: string) => void }) {
  const [projectId, setProjectId] = useState('');
  const [agentId, setAgentId] = useState('');
  const [agents, setAgents] = useState<Agent[]>([]);
  const [items, setItems] = useState<Memory[]>([]);
  const [skills, setSkills] = useState<Skill[]>([]);
  const [category, setCategory] = useState<'memories' | 'skills'>('memories');
  const [query, setQuery] = useState('');
  const [filter, setFilter] = useState('pending');
  const [error, setError] = useState('');
  const [edit, setEdit] = useState<Partial<Memory> | null>(null);
  const [forget, setForget] = useState<Memory | null>(null);
  const [skill, setSkill] = useState<Skill | null>(null);
  const [license, setLicense] = useState('');
  const [instructions, setInstructions] = useState('');
  const scope = { project_id: projectId || null, agent_id: agentId || null };
  async function refresh() {
    try {
      const result = await api<{ items: Memory[]; skills: Skill[] }>('memory_list', { ...scope, status: filter === 'all' ? undefined : filter });
      setItems(result.items); setSkills(result.skills); setError('');
    } catch (e) { setError(errorText(e)); }
  }
  useEffect(() => { void refresh(); }, [projectId, agentId, filter]);
  useEffect(() => { void api<{ agents: Agent[] }>('agents').then(r => setAgents(r.agents)).catch(e => notify(errorText(e))); }, []);
  async function action(name: string, data: Record<string, unknown>) {
    try { await api(name, { ...scope, ...data }); await refresh(); } catch (e) { notify(errorText(e)); }
  }
  async function save() {
    if (!edit) return;
    try {
      await api(edit.id ? 'memory_update' : 'memory_propose', { ...scope, title: edit.title, content: edit.content, kind: edit.kind, ...(edit.id ? { id: edit.id, revision: edit.revision } : { scope: agentId ? 'agent' : projectId ? 'project' : 'global', source: { kind: 'user' } }) });
      setEdit(null); await refresh();
    } catch (e) { setError(errorText(e)); }
  }
  async function exportMemory() {
    try {
      const result = await api('memory_export', scope);
      const url = URL.createObjectURL(new Blob([JSON.stringify(result, null, 2)], { type: 'application/json' }));
      const anchor = document.createElement('a'); anchor.href = url; anchor.download = 'forge-memory.json'; anchor.click(); URL.revokeObjectURL(url);
    } catch (e) { notify(errorText(e)); }
  }
  const search = query.trim().toLowerCase();
  const visibleItems = items.filter(m => (m.title + m.content).toLowerCase().includes(search));
  const selectedSkills = skills.filter(s => filter === 'all' || s.status === filter || filter === 'approved' && s.status === 'promoted');
  const visibleSkills = selectedSkills.filter(s => (s.name + s.description + s.markdown).toLowerCase().includes(search));
  const visibleCount = category === 'memories' ? visibleItems.length : visibleSkills.length;
  const reviewTitle = filter === 'pending' ? 'Awaiting review' : filter === 'approved' ? 'Approved' : filter === 'rejected' ? 'Rejected' : 'All saves';
  return <div className="memory-page">
    <PageHeader title="Memory" subtitle="Review useful facts and reusable skills.">
      <button className="secondary" onClick={() => { setError(''); setEdit({ title: '', content: '', kind: 'preference' }); }}><Plus size={14} />Suggest a save</button>
      <IconButton label="Refresh memory" onClick={() => void refresh()}><RefreshCw size={15} /></IconButton>
    </PageHeader>
    <div className="memory-workspace">
      <section className="memory-review" aria-label="Saved memory review">
        <header className="memory-review-header"><div><h2>{reviewTitle}</h2><span>{category === 'memories' ? 'Memories' : 'Learned skills'} · {visibleCount}</span></div></header>
        <div className="memory-toolbar">
          <div className="search-input"><Search size={14} /><input aria-label="Filter memory" placeholder={category === 'memories' ? 'Search memories' : 'Search learned skills'} value={query} onChange={e => setQuery(e.target.value)} /></div>
          <div className="memory-scope-controls">
            <select aria-label="Memory project" value={projectId} onChange={e => setProjectId(e.target.value)}><option value="">Global memory</option>{projects.map(p => <option value={p.id} key={p.id}>{p.name}</option>)}</select>
            <select aria-label="Memory agent" value={agentId} onChange={e => setAgentId(e.target.value)}><option value="">All general memories</option>{agents.map(a => <option value={a.id} key={a.id}>{a.name}</option>)}</select>
            <select aria-label="Memory status" value={filter} onChange={e => setFilter(e.target.value)}><option value="pending">Awaiting review</option><option value="approved">Approved</option><option value="rejected">Rejected</option><option value="all">All saves</option></select>
          </div>
        </div>
        {error && <ErrorNotice error={error} retry={() => void refresh()} />}
        <div className="memory-list">
          {category === 'memories' ? visibleItems.map(m => <article className="settings-card memory-card" key={m.id}>
            <div><Brain size={16} /><strong>{m.title}</strong><Badge>{m.scope}</Badge><Badge>{m.status}</Badge></div>
            <p>{m.content}</p><details><summary>Source & revision</summary><pre>{JSON.stringify(m.source, null, 2)}</pre><small>Revision {m.revision}</small></details>
            <footer><button className="text-button" onClick={() => { setError(''); setEdit(m); }}>Edit</button>{m.status === 'pending' && <><button className="secondary" onClick={() => void action('memory_review', { id: m.id, revision: m.revision, approved: true })}><Check size={13} />Approve</button><IconButton label={`Reject ${m.title}`} onClick={() => void action('memory_review', { id: m.id, revision: m.revision, approved: false })}><X size={14} /></IconButton></>}<IconButton label={`Forget ${m.title}`} onClick={() => setForget(m)}><Trash2 size={14} /></IconButton></footer>
          </article>) : visibleSkills.map(s => <article className="settings-card memory-card" key={s.id}>
            <div><FileText size={16} /><strong>{s.name}</strong><Badge>{s.scope}</Badge><Badge>{s.status}</Badge></div><p>{s.description}</p>
            {s.status === 'pending' && <footer><button className="secondary" onClick={() => { setSkill(s); setLicense(s.license.startsWith('Unspecified') ? '' : s.license); setInstructions(s.markdown.replace(/^---[\s\S]*?---\s*/, '')); }}>Review skill</button></footer>}
          </article>)}
          {!visibleCount && <Empty icon={category === 'memories' ? <Brain size={24} /> : <FileText size={24} />} title={search ? 'No matching saves' : filter === 'pending' ? `No ${category === 'memories' ? 'memories' : 'skills'} awaiting review` : `No ${category === 'memories' ? 'memories' : 'skills'} here`} text={search ? 'Try another search or change the filters.' : 'Approved saves stay scoped to the projects and agents you choose.'} />}
        </div>
      </section>
      <aside className="memory-controls" aria-label="Memory preferences">
        <nav className="memory-categories" aria-label="Memory categories">
          <button aria-pressed={category === 'memories'} className={category === 'memories' ? 'active' : ''} onClick={() => setCategory('memories')}><Brain size={16} /><span>Memories</span><Badge>{items.length}</Badge></button>
          <button aria-pressed={category === 'skills'} className={category === 'skills' ? 'active' : ''} onClick={() => setCategory('skills')}><FileText size={16} /><span>Learned skills</span><Badge>{selectedSkills.length}</Badge></button>
        </nav>
        <section className="settings-card memory-preferences-card">
          <h2>Memory & learning</h2>
          <Toggle label="Recall approved memory" checked={Boolean(settings.memory_enabled ?? true)} onChange={memory_enabled => void onChange({ memory_enabled })} />
          <Toggle label="Suggest useful saves" description="Facts and learned skills need your review before use." checked={Boolean(settings.memory_suggestions ?? true)} onChange={memory_suggestions => void onChange({ memory_suggestions })} />
        </section>
        <details className="settings-card memory-retrieval">
          <summary>Local retrieval</summary>
          <Toggle label="Use installed CPU embeddings" description="Optional. Keyword search works without extra downloads." checked={Boolean(settings.memory_semantic)} onChange={memory_semantic => void onChange({ memory_semantic })} />
          <Field label="Installed embedding model directory"><input value={String(settings.memory_model_path || '')} onChange={e => void onChange({ memory_model_path: e.target.value })} placeholder="Local model directory" /></Field>
        </details>
        <button className="text-button memory-export" onClick={() => void exportMemory()}>Export selected memory</button>
      </aside>
    </div>
    {edit && <Modal title={edit.id ? 'Edit saved memory' : 'Suggest a memory'} close={() => setEdit(null)}><form onSubmit={e => { e.preventDefault(); void save(); }}>{error && <ErrorNotice error={error} />}<Field label="Title"><input required value={edit.title || ''} onChange={e => setEdit({ ...edit, title: e.target.value })} /></Field><Field label="Type"><select value={edit.kind} onChange={e => setEdit({ ...edit, kind: e.target.value })}>{['fact', 'preference', 'convention'].map(k => <option key={k}>{k}</option>)}</select></Field><Field label="Content"><textarea rows={5} required value={edit.content || ''} onChange={e => setEdit({ ...edit, content: e.target.value })} /></Field><p className="muted">New suggestions need approval; confirming a correction replaces the saved text. Keep credentials in the operating system credential store.</p><footer><button type="button" className="subtle" onClick={() => setEdit(null)}>Cancel</button><button className="primary">{edit.id ? 'Save correction' : 'Save for review'}</button></footer></form></Modal>}
    {forget && <Modal title="Forget this memory?" close={() => setForget(null)}><p>{forget.title} will be removed from recall and its search indexes.</p><footer><button className="subtle" onClick={() => setForget(null)}>Cancel</button><button className="danger-button" onClick={() => { void action('memory_delete', { id: forget.id }); setForget(null); }}>Forget</button></footer></Modal>}
    {skill && <Modal title="Review learned skill" close={() => setSkill(null)} wide><Field label="Instructions"><textarea rows={12} value={instructions} onChange={e => setInstructions(e.target.value)} /></Field><Field label="Source license"><input value={license} onChange={e => setLicense(e.target.value)} placeholder="For example MIT, if you own these instructions" /></Field><p className="muted">Review the source, edit provisional guidance into reusable instructions, and confirm its license. Skills cannot expand permissions.</p><footer><button className="subtle" onClick={() => { void action('skill_promote', { id: skill.id, revision: skill.revision, approved: false }); setSkill(null); }}>Reject</button><button className="primary" disabled={!license.trim() || Boolean(skill.source?.note === 'provisional safe action template' && instructions.trim() === skill.markdown.replace(/^---[\s\S]*?---\s*/, '').trim())} onClick={() => void api('skill_promote', { ...scope, id: skill.id, revision: skill.revision, approved: true, instructions, license }).then(() => { setSkill(null); void refresh(); }).catch(e => notify(errorText(e)))}>Promote skill</button></footer></Modal>}
  </div>;
}
