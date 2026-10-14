import { SkillSelector } from './skillWorkbench';
import { ToolSelector } from './toolSelector';
import { useEffect, useRef, useState } from 'react';
import { Activity, ArrowUpRight, Bot, CalendarClock, Check, ClipboardList, Download, ExternalLink, Folder, FolderPlus, GitBranch, Layers3, LoaderCircle, Pause, Pencil, Play, Plug, Plus, RefreshCw, Trash2, Wrench, X } from 'lucide-react';
import { api, compactNumber, errorText, isNative, native, tokenLabel } from './api';
import { Badge, Empty, ErrorNotice, Field, IconButton, Markdown, Modal, PageHeader, Toggle, useResource } from './components';
import type { Agent, Chat, Goal, Model, Plugin, Project, Run, Schedule, Settings, Space, UsageCount } from './types';
import { AgentProviderFields } from './agentProviderFields';
import { GoalReviewCard } from './goalReviewCard';
import { GuidanceCard } from './taskStatus';
import { GoalTaskEvidence } from './goalTaskEvidence';
import { GoalGuidanceActions } from './goalGuidanceActions';
import { ProjectActions } from './projectActions';
import { ProjectIcon } from './appearance';
import './usage.css';

type Notify = (message: string) => void;
function Loading() { return <div className="loading"><LoaderCircle size={17} className="spin" />Loading…</div>; }

export function ProjectsPage({ projects, selected, open, refresh, notify }: { projects: Project[]; selected: string | null; open: (id: string | null) => void; refresh: () => Promise<void>; notify: Notify }) {
  const [adding, setAdding] = useState(false); const [managed, setManaged] = useState(false); const [name, setName] = useState(''); const [path, setPath] = useState(''); const [worktree, setWorktree] = useState<Project | null>(null); const [branch, setBranch] = useState(''); const [trees, setTrees] = useState<Record<string, unknown>[]>([]); const [review, setReview] = useState<{ id: string; diff: string; status?: string } | null>(null); const [busy, setBusy] = useState(false);
  async function add() {
    if (isNative()) { try { const project = await native<Project & { cancelled?: boolean }>('choose_project'); if (!project.cancelled) { await refresh(); open(project.id); } } catch (e) { notify(errorText(e)); } } else { setManaged(false); setAdding(true); }
  }
  async function save() {
    setBusy(true); try { const project = await api<Project>('create_project', managed ? { name, managed: true } : { path, name }); await refresh(); open(project.id); setAdding(false); setName(''); setPath(''); } catch (e) { notify(errorText(e)); } finally { setBusy(false); }
  }
  async function showTrees(project: Project) { setWorktree(project); setTrees([]); setReview(null); setBranch(''); try { const result = await api<{ worktrees: Record<string, unknown>[] }>('worktrees', { project_id: project.id }); setTrees(result.worktrees || []); } catch (e) { notify(errorText(e)); } }
  return <><PageHeader title="Projects" subtitle="Connect your folders and keep each workspace organized."><button className="secondary" onClick={() => { setManaged(true); setAdding(true); }}><Plus size={15} />New project</button><button className="primary" onClick={() => void add()}><FolderPlus size={15} />Open folder</button></PageHeader>{projects.length ? <div className="project-cards">{projects.map(project => <ProjectActions key={project.id} project={project} onRemoved={async id => { await refresh(); if (selected === id) open(null); notify('Project registration removed. Its folder was kept.'); }}><article className="project-card"><div className="project-card-icon"><ProjectIcon project={project} size={23}/></div><div><h2>{project.name}</h2><p title={project.path}>{project.path}</p>{project.branch && <Badge><GitBranch size={11} />{project.branch}</Badge>}</div><div className="project-card-actions"><button className="text-button" onClick={() => void showTrees(project)}><GitBranch size={14} />Worktrees</button><button className={selected === project.id ? 'primary' : 'secondary'} onClick={() => open(project.id)}>Open<ArrowUpRight size={13} /></button></div></article></ProjectActions>)}</div> : <Empty icon={<FolderPlus size={30} />} title="Start with a project" text="Connect an existing folder, or create a new project in your Forge workspace." action={<button className="primary" onClick={() => void add()}>Open folder</button>} />}
    {adding && <Modal title={managed ? 'New managed project' : 'Connect project folder'} close={() => setAdding(false)}><form onSubmit={e => { e.preventDefault(); void save(); }}><Field label="Project name"><input autoFocus value={name} onChange={e => setName(e.target.value)} placeholder="My project" required={managed} /></Field>{!managed && <Field label="Folder path"><input value={path} onChange={e => setPath(e.target.value)} placeholder="Absolute project folder path" required /></Field>}<p className="muted">{managed ? 'Forge creates this project in its managed projects folder.' : 'Your existing folder stays in place.'}</p><footer><button type="button" className="subtle" onClick={() => setAdding(false)}>Cancel</button><button className="primary" disabled={busy}>{busy ? 'Creating…' : managed ? 'Create project' : 'Connect'}</button></footer></form></Modal>}
    {worktree && <Modal title={`${worktree.name} · Worktrees`} close={() => setWorktree(null)} wide><p className="muted">Each writing agent can use an isolated branch. Review changes before integration.</p><div className="simple-list">{trees.map((tree, index) => <div key={index}><GitBranch size={16} /><div><strong>{String(tree.branch || 'Detached HEAD')}</strong><small>{String(tree.path || '')}</small></div><Badge>{tree.dirty ? 'Changes' : 'Clean'}</Badge><button className="text-button" onClick={() => void api('worktree_diff', { id: tree.id }).then(r => setReview({ id: String(tree.id), diff: String(r.diff || 'No changes'), status: String(r.status || '') })).catch(e => notify(errorText(e)))}>Diff</button></div>)}</div>{review && <div className="worktree-review"><h3>Review changes</h3><pre>{review.status}{review.diff}</pre><button className="primary" onClick={() => void api('worktree_integrate', { id: review.id }).then(r => { notify(String(r.message || 'Changes integrated.')); if (worktree) void showTrees(worktree); }).catch(e => notify(errorText(e)))}>Integrate reviewed changes</button></div>}<form className="inline-form" onSubmit={e => { e.preventDefault(); void api('worktree_create', { project_id: worktree.id, name: branch }).then(() => { notify('Worktree created.'); void showTrees(worktree); }).catch(e => notify(errorText(e))); }}><input aria-label="Worktree name" value={branch} onChange={e => setBranch(e.target.value)} placeholder="Worktree name" required /><button className="primary"><Plus size={14} />Create</button></form></Modal>}
  </>;
}

export function SpacesPage({ projects, notify }: { projects: Project[]; notify: Notify }) {
  const data = useResource<Space>('spaces', 'spaces'); const [edit, setEdit] = useState<Space | null>(null);
  async function save() { if (!edit) return; try { await api('space_save', { ...edit, id: edit.id || undefined }); setEdit(null); await data.refresh(); } catch (e) { notify(errorText(e)); } }
  return <><PageHeader title="Spaces" subtitle="Keep projects, notes and reference material together."><button className="primary" onClick={() => setEdit({ id: '', name: '', description: '', project_ids: [], notes: '' })}><Plus size={15} />New space</button></PageHeader>{data.error && <ErrorNotice error={data.error} retry={() => void data.refresh()} />}{data.loading ? <Loading /> : data.items.length ? <div className="card-grid">{data.items.map(space => <button className="space-card" key={space.id} onClick={() => setEdit(space)}><Layers3 size={25} /><h2>{space.name}</h2><p>{space.description || 'Projects and notes'}</p><small>{space.project_ids?.length || 0} projects</small></button>)}</div> : !data.error && <Empty icon={<Layers3 size={30} />} title="A place for the bigger picture" text="Create a space to group related projects and keep durable notes." />}
    {edit && <Modal title={edit.id ? 'Edit space' : 'New space'} close={() => setEdit(null)} wide><form onSubmit={e => { e.preventDefault(); void save(); }}><Field label="Name"><input autoFocus required value={edit.name} onChange={e => setEdit({ ...edit, name: e.target.value })} /></Field><Field label="Description"><input value={edit.description || ''} onChange={e => setEdit({ ...edit, description: e.target.value })} /></Field><Field label="Projects"><div className="checkbox-list">{projects.map(p => <label key={p.id}><input type="checkbox" checked={edit.project_ids?.includes(p.id) || false} onChange={e => setEdit({ ...edit, project_ids: e.target.checked ? [...(edit.project_ids || []), p.id] : (edit.project_ids || []).filter(id => id !== p.id) })} />{p.name}</label>)}</div></Field><Field label="Markdown notes"><textarea rows={7} value={edit.notes || ''} onChange={e => setEdit({ ...edit, notes: e.target.value })} placeholder="References, decisions and useful context…" /></Field><footer>{edit.id && <button type="button" className="danger text-button" onClick={() => void api('space_delete', { id: edit.id }).then(() => { setEdit(null); void data.refresh(); }).catch(e => notify(errorText(e)))}>Delete space</button>}<button className="primary">Save space</button></footer></form></Modal>}
  </>;
}

export function SchedulesPage({ projects, notify, onRun, onCount }: { projects: Project[]; notify: Notify; onRun: (run: Run) => void; onCount?: (count:number)=>void }) {
  const data = useResource<Schedule>('schedules', 'schedules'); const agents = useResource<Agent>('agents', 'agents'); const [edit, setEdit] = useState<Schedule | null>(null);
  useEffect(()=>{if(!data.loading&&!data.error)onCount?.(data.items.filter(item=>item.enabled!==false).length);},[data.items,data.loading,data.error,onCount]);
  const initial = (): Schedule => ({ id: '', name: '', prompt: '', recurrence: 'daily', timezone: Intl.DateTimeFormat().resolvedOptions().timeZone, enabled: true });
  async function save() { if (!edit) return; try { await api('schedule_save', { ...edit, id: edit.id || undefined }); setEdit(null); await data.refresh(); } catch (e) { notify(errorText(e)); } }
  return <><PageHeader title="Scheduled" subtitle="Run recurring work while the coordinator is open."><button className="primary" onClick={() => setEdit(initial())}><Plus size={15} />Schedule a task</button></PageHeader>{data.error && <ErrorNotice error={data.error} retry={() => void data.refresh()} />}{data.loading ? <Loading /> : data.items.length ? <div className="stack">{data.items.map(schedule => <article className="schedule-card" key={schedule.id}><div className="card-symbol"><CalendarClock size={22} /></div><div className="card-body"><h2>{schedule.name}</h2><p>{schedule.prompt}</p><small>{schedule.recurrence} · {schedule.timezone}{schedule.next_run ? ` · Next: ${new Date(schedule.next_run).toLocaleString()}` : ''}</small></div><Badge>{schedule.enabled ? 'Active' : 'Paused'}</Badge><IconButton label={`Run ${schedule.name} now`} onClick={() => void api<Run>('schedule_run', { id: schedule.id }).then(run => { onRun(run); notify('Scheduled task started.'); }).catch(e => notify(errorText(e)))}><Play size={15} /></IconButton><IconButton label={`Edit ${schedule.name}`} onClick={() => setEdit(schedule)}><Pencil size={15} /></IconButton></article>)}</div> : !data.error && <Empty icon={<CalendarClock size={30} />} title="Make room for routine work" text="Schedule project reviews, research and other recurring goals." />}
    {edit && <Modal title={edit.id ? 'Edit schedule' : 'Schedule a task'} close={() => setEdit(null)} wide><form onSubmit={e => { e.preventDefault(); void save(); }}><Field label="Name"><input autoFocus required value={edit.name} onChange={e => setEdit({ ...edit, name: e.target.value })} /></Field><Field label="Task"><textarea required rows={4} value={edit.prompt} onChange={e => setEdit({ ...edit, prompt: e.target.value })} placeholder="Review changes and summarize issues…" /></Field><div className="form-grid"><Field label="Project"><select value={edit.project_id || ''} onChange={e => setEdit({ ...edit, project_id: e.target.value || undefined })}><option value="">Personal workspace</option>{projects.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}</select></Field><Field label="Agent"><select value={edit.agent_id || ''} onChange={e => setEdit({ ...edit, agent_id: e.target.value || undefined })}><option value="">Main agent</option>{agents.items.map(a => <option key={a.id} value={a.id}>{a.name}</option>)}</select></Field><Field label="Repeat"><select value={edit.recurrence || 'daily'} onChange={e => setEdit({ ...edit, recurrence: e.target.value })}><option value="hourly">Every hour</option><option value="daily">Every day</option><option value="weekly">Every week</option></select></Field><Field label="Timezone"><input required value={edit.timezone || ''} onChange={e => setEdit({ ...edit, timezone: e.target.value })} /></Field></div><div className="form-grid"><Field label="Time"><input type="time" required value={String(edit.time || '09:00')} onChange={e => setEdit({ ...edit, time: e.target.value })} /></Field><Field label="Permission profile"><select value={String(edit.permission_profile || 'always_ask')} onChange={e => setEdit({ ...edit, permission_profile: e.target.value })}><option value="always_ask">Always Ask</option><option value="full_access">Full Access within global policy</option><option value="deny_access">Deny Access</option></select></Field>{edit.recurrence === 'weekly' && <Field label="Weekday"><select value={Number(edit.weekday ?? 0)} onChange={e => setEdit({ ...edit, weekday: Number(e.target.value) })}>{['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'].map((day,index) => <option key={day} value={index}>{day}</option>)}</select></Field>}</div><Toggle label="Schedule enabled" checked={Boolean(edit.enabled)} onChange={enabled => setEdit({ ...edit, enabled })} /><footer>{edit.id && <button type="button" className="danger text-button" onClick={() => void api('schedule_delete', { id: edit.id }).then(() => { setEdit(null); void data.refresh(); }).catch(e => notify(errorText(e)))}>Delete</button>}<button className="primary">Save schedule</button></footer></form></Modal>}
  </>;
}

export function AgentsPage({ models, projects, projectId, settings, notify, onRun }: { models: Model[]; projects: Project[]; projectId: string | null; settings: Settings; notify: Notify; onRun: (run: Run) => void }) {
  const data = useResource<Agent>('agents', 'agents'); const [edit, setEdit] = useState<Agent | null>(null); const [launch, setLaunch] = useState<Agent | null>(null); const [prompt, setPrompt] = useState(''); const [target, setTarget] = useState(projectId || ''); const [busy, setBusy] = useState(false);
  async function save() { if (!edit) return; try { await api('agent_save', { ...edit, id: edit.id || undefined }); setEdit(null); await data.refresh(); } catch (e) { notify(errorText(e)); } }
  async function start() { if (!launch) return; setBusy(true); try { const run = await api<Run>('agent_start', { agent_id: launch.id, text: prompt, project_id: target || null, ...settings }); onRun(run); setLaunch(null); notify(`${launch.name} started. Follow progress in Activity.`); } catch (e) { notify(errorText(e)); } finally { setBusy(false); } }
  return <><PageHeader title="Agents" subtitle="Delegate focused work to profiles you control."><button className="primary" onClick={() => setEdit({ id: '', name: '', instructions: '', role: 'researcher', context: settings.context, enabled: true, tools: [], skills: [] })}><Plus size={15} />New agent</button></PageHeader>{data.error && <ErrorNotice error={data.error} retry={() => void data.refresh()} />}{data.loading ? <Loading /> : <div className="card-grid">{data.items.map(agent => <article className="agent-card" key={agent.id}><div className="agent-card-head"><div className="card-symbol"><Bot size={23} /></div><Badge tone={agent.enabled ? '' : 'muted'}>{agent.enabled ? 'Enabled' : 'Disabled'}</Badge></div><h2>{agent.name}</h2><p>{agent.instructions}</p><div className="agent-meta"><span>{agent.model || 'Inherit model'}</span><span>{tokenLabel(agent.context || settings.context)} context</span></div><footer><button className="text-button" onClick={() => setEdit(agent)}><Pencil size={13} />Configure</button><button className="secondary" disabled={!agent.enabled} onClick={() => { setLaunch(agent); setPrompt(''); setTarget(projectId || ''); }}><Play size={13} />Launch</button></footer></article>)}</div>}
    {edit && <Modal title={edit.id ? `Configure ${edit.name}` : 'New agent'} close={() => setEdit(null)} wide><form onSubmit={e => { e.preventDefault(); void save(); }}><Field label="Name"><input autoFocus required value={edit.name} onChange={e => setEdit({ ...edit, name: e.target.value })} /></Field><Field label="Role"><select value={String(edit.role || 'researcher')} onChange={e => setEdit({ ...edit, role: e.target.value })}><option value="researcher">Researcher · read only</option><option value="coder">Coder · writes in a worktree</option><option value="reviewer">Reviewer · read only</option></select></Field><Field label="Instructions"><textarea required rows={6} value={edit.instructions} onChange={e => setEdit({ ...edit, instructions: e.target.value })} /></Field><div className="form-grid"><AgentProviderFields agent={edit} models={models} update={setEdit} /><Field label={`Context · ${tokenLabel(edit.context || settings.context)}`}><input type="range" min="2048" max="262144" step="2048" value={edit.context || settings.context} onChange={e => setEdit({ ...edit, context: Number(e.target.value) })} /></Field></div><ToolSelector projectId={projectId} value={edit.tools || []} onChange={tools=>setEdit({...edit,tools})}/><SkillSelector projectId={projectId} label="Profile skills" value={edit.skills || []} onChange={skills=>setEdit({...edit,skills})}/><Field label="Specialty"><select value={String(edit.specialty || "")} onChange={e=>setEdit({...edit,specialty:e.target.value})}><option value="">General profile</option>{["planner","design","coding-advice","diagnosis","research","review"].map(value=><option key={value} value={value}>{value}</option>)}</select></Field><h3>Standalone goal limits</h3><div className="form-grid">{[["minutes", "Minutes", 60], ["tokens", "Generated tokens", 100000], ["rounds", "Model rounds", 128], ["tools", "Tool invocations", 256]].map(([key,label,fallback]) => <Field key={String(key)} label={String(label)}><input type="number" min="1" value={Number((edit.goal_limits as Record<string,number> | undefined)?.[String(key)] || fallback)} onChange={e => setEdit({ ...edit, goal_limits: { ...(edit.goal_limits as Record<string,number> || {}), [String(key)]: Number(e.target.value) } })} /></Field>)}</div><p className="muted">Child agents share their parent goal's allowance.</p><Toggle label="Enable this agent" checked={Boolean(edit.enabled)} onChange={enabled => setEdit({ ...edit, enabled })} /><footer>{edit.id && <button type="button" className="danger text-button" onClick={() => void api('agent_delete', { id: edit.id }).then(() => { setEdit(null); void data.refresh(); }).catch(e => notify(errorText(e)))}>Delete profile</button>}<button className="primary">Save profile</button></footer></form></Modal>}
    {launch && <Modal title={`Launch ${launch.name}`} close={() => setLaunch(null)}><form onSubmit={e => { e.preventDefault(); void start(); }}><Field label="Task"><textarea autoFocus required rows={5} value={prompt} onChange={e => setPrompt(e.target.value)} /></Field><Field label="Project"><select value={target} onChange={e => setTarget(e.target.value)}><option value="">Personal workspace</option>{projects.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}</select></Field><p className="muted">The agent inherits your permission profile. Writing tasks use isolated Git worktrees where available.</p><footer><button className="primary" disabled={busy}>{busy ? 'Starting…' : 'Launch agent'}</button></footer></form></Modal>}
  </>;
}


interface UsageResponse { totals: UsageCount & { estimated_requests?: number }; daily: { date: string; input_tokens: number; output_tokens: number }[]; by_model?: (UsageCount & { model: string })[]; average_tps?: number; tracked_since?: string; timezone?: string; }
export function UsagePage({ projects, models, timezone }: { projects: Project[]; models: Model[]; timezone: string }) {
  const [period, setPeriod] = useState('month'); const [model, setModel] = useState(''); const [project, setProject] = useState('');
  const projectName = useRef('Unavailable project');
  const selectedProject = projects.find(item => item.id === project);
  if (selectedProject) projectName.current = selectedProject.name;
  const bridge = window.pywebview?.api, bridgeCall = bridge?.call, transport = bridgeCall || window.fetch;
  const key = JSON.stringify([period, model, project, timezone]);
  const owner = useRef({ key, bridge, bridgeCall, transport });
  if (owner.current.key !== key || owner.current.bridge !== bridge || owner.current.bridgeCall !== bridgeCall || owner.current.transport !== transport) owner.current = { key, bridge, bridgeCall, transport };
  const scope = owner.current, mounted = useRef(false), request = useRef(0);
  const [read, setRead] = useState<{ owner: typeof scope; result: UsageResponse | null; totals: Record<string, UsageCount>; error: string; loading: boolean }>({ owner: scope, result: null, totals: {}, error: '', loading: true });
  async function refresh() {
    const ticket = ++request.current;
    const accepted = () => mounted.current && owner.current === scope && ticket === request.current
      && window.pywebview?.api === scope.bridge && window.pywebview?.api?.call === scope.bridgeCall
      && (window.pywebview?.api?.call || window.fetch) === scope.transport;
    if (!accepted()) return;
    setRead(current => accepted() ? { owner: scope, result: current.owner === scope ? current.result : null, totals: current.owner === scope ? current.totals : {}, error: '', loading: true } : current);
    try {
      const [selected, ...cards] = await Promise.all([api<UsageResponse>('usage', { period, timezone, model, project_id: project || undefined }), ...['day', 'month', 'all'].map(p => api<UsageResponse>('usage', { period: p, timezone, model, project_id: project || undefined }))]);
      if (!accepted()) return;
      setRead(current => accepted() ? { owner: scope, result: selected, totals: { day: cards[0].totals, month: cards[1].totals, all: cards[2].totals }, error: '', loading: true } : current);
    } catch (e) {
      if (!accepted()) return;
      setRead(current => accepted() ? { ...current, error: errorText(e) } : current);
    } finally {
      if (accepted()) setRead(current => accepted() ? { ...current, loading: false } : current);
    }
  }
  useEffect(() => {
    mounted.current = true;
    void refresh();
    return () => { mounted.current = false; request.current++; };
  }, [period, model, project, timezone, bridge, bridgeCall, transport]);
  const result = read.owner === scope ? read.result : null, totals = read.owner === scope ? read.totals : {};
  const error = read.owner === scope ? read.error : '', loading = read.owner !== scope || read.loading;
  const daily = result?.daily || [], visibleDaily = daily.slice(-31);
  const max = Math.max(1, ...visibleDaily.map(d => d.input_tokens + d.output_tokens));
  const formatDate = (value: string) => new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC' }).format(new Date(value + 'T12:00:00Z'));
  const dateRange = visibleDaily.length ? `${formatDate(visibleDaily[0].date)} – ${formatDate(visibleDaily[visibleDaily.length - 1].date)}` : '';
  const recorded = Boolean(result && (result.totals.input_tokens || result.totals.output_tokens || result.totals.cached_input_tokens || result.totals.requests));
  return <section className="usage-page" aria-label="Usage accounting" aria-busy={loading}>
    <PageHeader title="Usage" subtitle="Local token accounting across every agent and inference request."><IconButton label="Refresh usage" showLabel onClick={() => void refresh()}><RefreshCw size={16} /></IconButton></PageHeader>
    {error && <ErrorNotice error={error} retry={() => void refresh()} />}
    {loading && result && <p className="usage-read-status" role="status">Refreshing usage… Previously loaded totals remain below.</p>}
    {error && result && <p className="usage-read-status">Showing previously loaded totals. Refresh did not finish.</p>}
    <div className="usage-cards">{[['day', 'Today'], ['month', 'This month'], ['all', 'All time']].map(([value, label]) => <article key={value} aria-label={label}><span>{label}</span><strong>{result ? compactNumber((totals[value]?.input_tokens || 0) + (totals[value]?.output_tokens || 0)) : '—'}</strong><small>{result ? <>{compactNumber(totals[value]?.output_tokens || 0)} output · {totals[value]?.requests || 0} requests</> : loading ? 'Loading totals…' : 'Totals unavailable'}</small></article>)}</div>
    <div className="usage-chart-card">
      <div className="usage-toolbar">
        <div className="segmented" role="group" aria-label="Usage period">{[['day', 'Day'], ['month', 'Month'], ['all', 'All time']].map(([value, label]) => <button key={value} aria-pressed={period === value} className={period === value ? 'active' : ''} onClick={() => setPeriod(value)}>{label}</button>)}</div>
        <div className="usage-filters"><label><span>Model</span><select aria-label="Filter usage by model" value={model} onChange={e => setModel(e.target.value)}><option value="">All models</option>{model && !models.some(item => item.name === model) && <option value={model}>{model} (unavailable)</option>}{models.map(m => <option key={m.name}>{m.name}</option>)}</select></label><label><span>Project</span><select aria-label="Filter usage by project" value={project} onChange={e => setProject(e.target.value)}><option value="">All projects</option>{project && !selectedProject && <option value={project}>{projectName.current} (unavailable)</option>}{projects.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}</select></label></div>
      </div>
      {visibleDaily.length > 0 && <div className="usage-chart-heading"><div><h2>Daily tokens</h2><p>{dateRange}</p></div>{daily.length > 31 && <span>Latest 31 recorded days</span>}</div>}
      {!result && loading ? <div role="status"><Loading /></div> : !result ? <Empty icon={<Activity size={27} />} title="Usage unavailable" text="Retry to load accounting for the current filters." /> : daily.length ? <div className="usage-chart" role="img" aria-label={`Daily input and output token usage, ${dateRange}`}><div className="chart-bars">{visibleDaily.map((d, index) => <div className="chart-column" key={d.date} title={`${d.date}: ${d.input_tokens.toLocaleString()} input, ${d.output_tokens.toLocaleString()} output`}><div className="bar-stack" style={{ height: `${Math.max(2, (d.input_tokens + d.output_tokens) / max * 100)}%` }}><div className="output-bar" style={{ flex: d.output_tokens || 0 }} /><div className="input-bar" style={{ flex: d.input_tokens || 0 }} /></div>{(index === 0 || index === visibleDaily.length - 1 || index % Math.ceil(visibleDaily.length / 7) === 0) && <span>{d.date.slice(-2)}</span>}</div>)}</div><div className="chart-legend"><span><i className="input-legend" />Input</span><span><i className="output-legend" />Output</span></div></div> : <Empty icon={<Activity size={27} />} title={recorded ? 'No daily breakdown' : 'No usage in this period'} text={recorded ? 'Recorded totals are shown below. No daily entries are available for this period and these filters.' : 'There are no recorded requests for this period and the selected filters.'} />}
      <div className="usage-breakdown"><div><span>Input tokens</span><strong>{result ? (result.totals.input_tokens || 0).toLocaleString() : '—'}</strong></div><div><span>Output tokens</span><strong>{result ? (result.totals.output_tokens || 0).toLocaleString() : '—'}</strong></div><div><span>Cached input</span><strong>{result ? (result.totals.cached_input_tokens || 0).toLocaleString() : '—'}</strong></div><div><span>Generation speed</span><strong>{result?.average_tps ? `${result.average_tps.toFixed(1)} tok/s` : '—'}</strong></div></div>
    </div>
    <p className="page-footnote">Calendar periods use {timezone}. {result ? <>{result.totals.estimated_requests ? `${result.totals.estimated_requests} requests use estimated counts. ` : ''}{result.tracked_since ? `Tracking since ${new Date(result.tracked_since).toLocaleDateString()}.` : 'Usage begins with Forge 4.0.'}</> : loading ? 'Loading usage details…' : 'Usage details are unavailable.'}</p>
    {result?.by_model?.length ? <div className="table-card usage-model-table"><table><caption>By model</caption><thead><tr><th scope="col">Model</th><th scope="col">Input</th><th scope="col">Output</th><th scope="col">Requests</th></tr></thead><tbody>{result.by_model.map(m => <tr key={m.model}><td>{m.model}</td><td>{m.input_tokens.toLocaleString()}</td><td>{m.output_tokens.toLocaleString()}</td><td>{m.requests.toLocaleString()}</td></tr>)}</tbody></table></div> : null}
  </section>;
}

export function activeGoals(goals: Goal[], chats: Chat[]) {
  const active = new Set(['queued', 'running', 'compacting', 'reviewing', 'starting', 'resuming', 'waiting_approval', 'awaiting_approval', 'waiting_question', 'awaiting_question']);
  const chatIds = new Set(chats.map(chat => chat.id));
  return goals.filter(goal => Boolean(goal.chat_id && chatIds.has(goal.chat_id)) && (active.has(goal.status) || ['paused','interrupted'].includes(goal.status) && (goal.execution_mode==='guided' || Boolean(goal.review && ['needs_changes','insufficient_evidence','error','reviewing'].includes(goal.review.status)))));
}
export function goalInProject(goal: Goal, projectId: string | null, chats: Chat[]) {
  const chat = chats.find(item => item.id === goal.chat_id);
  if (!chat || (chat.project_id || null) !== projectId) return false;
  const declared = (goal as Goal & { project_id?: string | null }).project_id;
  return declared === undefined || declared === projectId;
}
function goalDetailMarker(goal: Goal) {
  const hash = (goal as Goal & { markdown_hash?: string }).markdown_hash;
  return JSON.stringify([goal.revision, hash, goal.run_id, goal.status, goal.review?.status, hash === undefined ? goal.markdown : undefined]);
}
function olderGoal(incoming: Goal, existing: Goal) {
  return typeof incoming.revision === 'number' && typeof existing.revision === 'number' && Number.isSafeInteger(incoming.revision) && Number.isSafeInteger(existing.revision) && incoming.revision < existing.revision;
}
export function GoalsPanel({ projectId, chats, version, notify, onRun }: { projectId: string | null; chats: Chat[]; version: number; notify: Notify; onRun: (run: Run) => void }) {
  const data = useResource<Goal>('goals', 'goals', { project_id: projectId, active_only: true }); const [selected, setSelected] = useState<Goal | null>(null); const [markdown, setMarkdown] = useState(''); const [conflict, setConflict] = useState(false);
  const [opening, setOpening] = useState<{ id: string; projectId: string | null } | null>(null), [mutating, setMutating] = useState(false);
  const [refreshingDetail, setRefreshingDetail] = useState(false), [detailError, setDetailError] = useState('');
  const detailRequest = useRef(0), detailMarker = useRef('');
  const mounted = useRef(true), request = useRef(0), mutationPending = useRef(false), context = useRef({ projectId, chats });
  const priorProject = useRef(projectId), buttons = useRef(new Map<string, HTMLButtonElement>());
  context.current = { projectId, chats };
  if (priorProject.current !== projectId) { priorProject.current = projectId; request.current++; }
  const visible = activeGoals(data.items.filter(goal => goalInProject(goal, projectId, chats)), chats);
  const listed = selected && visible.find(goal => goal.id === selected.id);
  const current = selected && listed && goalInProject(selected, projectId, chats) ? olderGoal(listed, selected) ? selected : { ...selected, ...listed } : null;
  const currentGoal = useRef<Goal | null>(null); currentGoal.current = current;
  const pendingGoal = opening?.projectId === projectId ? visible.find(goal => goal.id === opening.id) : undefined;
  const selectionGeneration = request.current;
  const controlsPending = mutating || refreshingDetail || Boolean(detailError);
  const shownMarkdown = markdown;
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; request.current++; }; }, []);
  useEffect(() => { detailRequest.current++; detailMarker.current = ''; setSelected(null); setOpening(null); setMarkdown(''); setConflict(false); setRefreshingDetail(false); setDetailError(''); }, [projectId]);
  useEffect(() => { let pending=false; const refresh=async()=>{if(pending || document.visibilityState==='hidden')return;pending=true;try{await data.refresh();}finally{pending=false;}};void refresh(); const timer = setInterval(() => void refresh(), 2000); return () => clearInterval(timer); }, [version, projectId]);
  useEffect(() => {
    if (!data.loading && (selected && !visible.some(goal => goal.id === selected.id) || opening && !pendingGoal)) {
      request.current++; setSelected(null); setOpening(null); setMarkdown(''); setConflict(false);
    }
  }, [data.items, chats, data.loading, selected, opening]);
  function owned(identity: number, scope: string | null, goal: Goal) {
    return mounted.current && identity === request.current && context.current.projectId === scope && goalInProject(goal, scope, context.current.chats);
  }
  async function open(goal: Goal) {
    if (!goalInProject(goal, projectId, chats)) return;
    const identity = ++request.current, scope = projectId;
    detailRequest.current++; setRefreshingDetail(false); setDetailError('');
    setSelected(null); setOpening({ id: goal.id, projectId: scope }); setMarkdown(''); setConflict(false);
    try {
      const r = await api<{ goal: Goal; markdown: string; conflict?: boolean }>('goal_get', { id: goal.id, project_id: scope });
      if (!owned(identity, scope, goal)) return;
      if (r.goal.id !== goal.id || !goalInProject(r.goal, scope, context.current.chats)) throw new Error('This goal is no longer available in this project.');
      if (!activeGoals([r.goal], context.current.chats).length) { void data.refresh(); return; }
      detailMarker.current = goalDetailMarker({ ...goal, ...r.goal });
      data.setItems(items => items.map(item => item.id === goal.id && !olderGoal(r.goal, item) ? { ...item, ...r.goal } : item));
      setSelected(r.goal); setMarkdown(r.markdown); setConflict(Boolean(r.conflict || (r.goal as Goal & { external_edits?: boolean }).external_edits));
    } catch (e) { if (owned(identity, scope, goal)) notify(errorText(e)); }
    finally { if (owned(identity, scope, goal)) setOpening(null); }
  }
  function close() {
    const id = current?.id || pendingGoal?.id;
    request.current++; detailRequest.current++; setRefreshingDetail(false); setDetailError(''); setSelected(null); setOpening(null); setMarkdown(''); setConflict(false);
    if (id) queueMicrotask(() => buttons.current.get(id)?.focus());
  }
  function changed(goal: Goal) {
    if (!current || !owned(selectionGeneration, projectId, current) || current.id !== goal.id || !goalInProject(goal, projectId, context.current.chats)) return;
    const latest = currentGoal.current;
    if (!latest || latest.id !== goal.id || latest.run_id && goal.run_id !== latest.run_id || olderGoal(goal, latest)) return;
    detailRequest.current++; setRefreshingDetail(false); setDetailError(''); detailMarker.current = goalDetailMarker({ ...current, ...goal });
    setSelected(goal); setMarkdown(goal.markdown || ''); data.setItems(items => items.map(item => item.id === goal.id && !olderGoal(goal, item) ? { ...item, ...goal } : item)); void data.refresh();
  }
  async function refreshDetail(goal: Goal) {
    const identity = request.current, scope = projectId, detail = ++detailRequest.current;
    setRefreshingDetail(true); setDetailError('');
    try {
      const result = await api<{ goal: Goal; markdown: string; conflict?: boolean }>('goal_get', { id: goal.id, project_id: scope });
      if (!owned(identity, scope, goal) || detail !== detailRequest.current) return;
      if (result.goal.id !== goal.id || !goalInProject(result.goal, scope, context.current.chats)) throw new Error('This goal is no longer available in this project.');
      if (!activeGoals([result.goal], context.current.chats).length) { setSelected(null); void data.refresh(); return; }
      setSelected(result.goal); setMarkdown(result.markdown); setConflict(Boolean(result.conflict || (result.goal as Goal & { external_edits?: boolean }).external_edits));
    } catch (e) { if (owned(identity, scope, goal) && detail === detailRequest.current) setDetailError(errorText(e)); }
    finally { if (owned(identity, scope, goal) && detail === detailRequest.current) setRefreshingDetail(false); }
  }
  const listMarker = listed ? goalDetailMarker(listed) : '';
  useEffect(() => {
    if (!current || !listMarker || listMarker === detailMarker.current) return;
    detailMarker.current = listMarker;
    void refreshDetail(current);
  }, [listMarker, selected?.id]);
  async function mutate(action: 'goal_resume' | 'goal_pause' | 'goal_export' | 'goal_reconcile', mode?: string) {
    if (!current || mutationPending.current || !goalInProject(current, projectId, context.current.chats)) return;
    const goal = current, identity = request.current, scope = projectId;
    mutationPending.current = true; setMutating(true);
    try {
      const result = await api<Run & { path?: string }>(action, { id: goal.id, ...(action === 'goal_export' ? { project_id: scope } : {}), ...(mode ? { mode: mode === 'restore' ? 'overwrite' : mode } : {}) });
      if (!owned(identity, scope, goal)) return;
      if (action === 'goal_resume') onRun(result);
      if (action === 'goal_export') notify(result.path || 'Checklist exported.');
      else if (action === 'goal_reconcile') { notify('Checklist reconciled.'); await open(goal); }
      else void data.refresh();
    } catch (e) { if (owned(identity, scope, goal)) notify(errorText(e)); }
    finally { mutationPending.current = false; if (mounted.current) setMutating(false); }
  }
  return <div className="goals-panel">{data.error && <ErrorNotice error={data.error} retry={() => void data.refresh()} />}<h3>Active goals</h3>
    {data.loading && !visible.length && <p className="muted" role="status">Loading goals…</p>}
    {!data.loading && !data.error && !visible.length && <Empty icon={<ClipboardList size={24} />} title="No active goals" text="Build a plan or use /goal to start tracked work here." />}
    {visible.map(goal => <button key={goal.id} ref={element => { if (element) buttons.current.set(goal.id, element); else buttons.current.delete(goal.id); }} className="goal-item" disabled={mutating} aria-expanded={current?.id === goal.id || pendingGoal?.id === goal.id} onClick={() => void open(goal)}><ClipboardList size={14} /><span>{goal.title || goal.text || 'Goal'}</span><Badge>{goal.status}</Badge></button>)}
    {(current || pendingGoal) && <div className="goal-detail" aria-busy={Boolean(pendingGoal) || mutating || refreshingDetail}><header><strong>{current?.title || pendingGoal?.title || 'Goal checkpoint'}</strong><IconButton label="Close goal detail" disabled={mutating} onClick={close}><X size={14} /></IconButton></header>
      {pendingGoal && <p className="muted" role="status">Loading goal…</p>}
      {current && <>{refreshingDetail && <p className="muted" role="status">Refreshing checklist…</p>}{detailError && <ErrorNotice error={detailError} retry={() => void refreshDetail(current)}/>}<div inert={controlsPending || undefined}>{conflict && <div className="conflict-banner"><strong>Checklist changed outside Forge</strong><p>Choose which version to keep before continuing.</p><button disabled={controlsPending} onClick={() => void mutate('goal_reconcile', 'import')}>Use Markdown edits</button><button disabled={controlsPending} onClick={() => void mutate('goal_reconcile', 'restore')}>Restore saved state</button></div>}<GuidanceCard assignment={current.planner_assignment}/><GoalGuidanceActions goal={current} conflict={conflict} notify={notify} onRequested={changed}/><GoalReviewCard review={current.review} paused={["paused","interrupted"].includes(current.status)} /><Markdown text={shownMarkdown} /><GoalTaskEvidence goal={current} conflict={conflict} onAccepted={changed}/></div>
        <div className="goal-controls"><button className="secondary" disabled={controlsPending || conflict || !["paused","interrupted","failed","ready"].includes(current.status)} onClick={() => void mutate('goal_resume')}><Play size={13} />Resume</button><button className="text-button" disabled={controlsPending || !['queued','running','compacting','reviewing','starting','resuming','waiting_approval','awaiting_approval','waiting_question','awaiting_question'].includes(current.status)} onClick={() => void mutate('goal_pause')}><Pause size={13} />Pause</button><button className="text-button" disabled={controlsPending || projectId === null} onClick={() => void mutate('goal_export')}><Download size={13} />Export</button></div>{projectId === null && <p className="muted">Connect this goal to a project to export its checklist.</p>}{current.path && <small className="goal-path">{current.path}</small>}</>}
    </div>}
  </div>;
}
