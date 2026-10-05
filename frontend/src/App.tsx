import { lazy, Suspense, useCallback, useEffect, useRef, useState } from 'react';
import { Info, Bell, Brain, Activity, ArrowUp, AudioLines, Bot, Check, ChevronDown, ChevronRight, Clock3, Command, Expand, FileCode2, Folder, FolderPlus, GitBranch, Globe, Layers3, LoaderCircle, Maximize2, MessageSquare, Mic, Minimize2, PanelLeftClose, PanelLeftOpen, PanelRightClose, PanelRightOpen, Pause, Play, Plug, Plus, Search, Settings2, ShieldCheck, Square, Terminal, Waypoints, X } from 'lucide-react';
import { api, errorText, friendlyModel, isNative, listFrom, native, tokenLabel } from './api';
import { Badge, Empty, IconButton, Markdown, MessageView, ModelPicker, Modal, SlashPalette, ToolCard, slashCommands } from './components';
import { AgentsPage, GoalsPanel, PluginsPage, ProjectsPage, SchedulesPage, SettingsPage, SpacesPage, UsagePage } from './pages';
import { ContextControl } from './contextControl';
import { RecoveryPanel } from './recoveryPanel';
import { WorkspaceSidebar } from './workspaceSidebar';
import { QuestionPanel } from './questionPanel';
import type { ChatChange } from './chatActions';
import { DEFAULT_SETTINGS } from './types';
import type { Approval, Chat, Message, Model, Page, Project, Run, RunEvent, SavedChat, Settings } from './types';

const BrowserPanel = lazy(() => import('./browserPanel').then(m => ({ default: m.BrowserPanel })));
const SetupWizard = lazy(() => import('./setupWizard').then(m => ({ default: m.SetupWizard })));
const MemoryPage = lazy(() => import('./memoryPage').then(m => ({ default: m.MemoryPage })));
const NotificationCenter = lazy(() => import('./channelSettings').then(m => ({ default: m.NotificationCenter })));

interface LiveRun { run: Run; text: string; thinking: string; parts: Message[]; tools: RunEvent[]; cursor: number; status: string; finished: boolean; currentPersisted?: boolean; speed?: number; speedEstimated?: boolean; firstToken?: number; approval?: Approval; }
interface SavedPlan { id: string; run_id: string; chat_id: string; project_id?: string; request: string; status: string; markdown: string; tasks: { text: string; status: string }[]; path?: string; }
const ACTIVE = new Set(['queued', 'running', 'compacting', 'waiting_approval', 'awaiting_approval', 'waiting_question', 'awaiting_question', 'starting', 'resuming']);
const navItems: { page: Page; label: string; icon: typeof Folder }[] = [
  { page: 'projects', label: 'Projects', icon: Folder }, { page: 'spaces', label: 'Spaces', icon: Layers3 }, { page: 'scheduled', label: 'Scheduled', icon: Clock3 }, { page: 'plugins', label: 'Plugins', icon: Plug }, { page: 'agents', label: 'Agents', icon: Bot }, { page: 'usage', label: 'Usage', icon: Activity }, { page: 'memory', label: 'Memory', icon: Brain },
];

function applyEvents(live: LiveRun, events: RunEvent[], result: { next_cursor?: number; status?: string; finished?: boolean; recovery?: string }): LiveRun {
  const next = { ...live, tools: [...live.tools], parts: [...live.parts] };
  let completedEvent = false;
  for (const event of events) {
    if (event.seq !== undefined && event.seq <= live.cursor) continue;
    if (event.seq !== undefined) next.cursor = Math.max(next.cursor, event.seq);
    if (event.type === 'token') { next.text += event.text || ''; next.currentPersisted = false; next.firstToken ||= Date.now(); next.status = 'Generating'; }
    else if (event.type === 'thinking') { next.thinking += event.text || ''; next.currentPersisted = false; next.status = 'Thinking'; }
    else if (event.type === 'round') {
      if (next.text || next.thinking) next.parts.push({ role: 'assistant', content: next.text, thinking: next.thinking });
      next.text = ''; next.thinking = '';
      next.currentPersisted = false;
      next.speed = undefined; next.speedEstimated = undefined; next.firstToken = undefined;
    } else if (event.type === 'tool') { const previous = event.invocation_id ? next.tools.findIndex(item => item.invocation_id === event.invocation_id) : -1; if (previous >= 0) next.tools[previous] = { ...next.tools[previous], ...event }; else next.tools.push(event); next.currentPersisted = true; next.status = `${event.state === 'running' ? 'Using' : 'Completed'} ${event.name || 'tool'}`; }
    else if (event.type === 'approval') { next.approval = { job_id: live.run.id, approval_id: String(event.approval_id || event.id || ''), name: String(event.name || event.tool || 'Tool action'), arguments: event.arguments, command: event.command }; next.currentPersisted = true; next.status = 'Approval needed'; }
    else if (event.type === 'question') { next.currentPersisted = true; next.status = 'Waiting for your answers'; }
    else if (event.type === 'question_answered') next.status = 'Continuing with your answers';
    else if (event.type === 'steer') { const applied = (event.state || event.status) === 'applied'; next.status = applied ? 'Following your direction' : 'Direction queued for the next safe step'; if (applied) next.currentPersisted = true; }
    else if (event.type === 'status' || event.type === 'memory' || event.type === 'context') next.status = event.text?.startsWith('Working') && !next.firstToken ? 'Loading model / waiting for engine…' : event.text || next.status;
    else if (event.type === 'stream_speed' || event.type === 'usage' || event.type === 'generation') { const raw = event.tokens_per_second ?? event.tps; const tps = Number(raw); if (raw !== null && raw !== undefined && Number.isFinite(tps)) { next.speed = tps; next.speedEstimated = Boolean(event.estimated); } }
    else if (event.type === 'error') next.status = event.text || 'Run needs attention';
    else if (event.type === 'outcome_unknown') { const previous = event.invocation_id ? next.tools.findIndex(item => item.invocation_id === event.invocation_id) : -1; const update = { ...event, state: 'Outcome unknown' }; if (previous >= 0) next.tools[previous] = { ...next.tools[previous], ...update }; else next.tools.push(update); next.status = 'Inspect interrupted action'; }
    else if (event.type === 'done') { completedEvent = true; next.finished = true; next.currentPersisted = true; next.status = event.cancelled ? 'Cancelled' : 'Completed'; next.approval = undefined; }
  }
  next.cursor = Math.max(next.cursor, result.next_cursor || 0);
  // The current coordinator state takes precedence over earlier turns in a replay page.
  if (typeof result.finished === 'boolean') next.finished = result.finished;
  if (next.finished) next.tools = next.tools.map(tool => tool.state === 'running' ? { ...tool, state: 'Interrupted' } : tool);
  if (result.status) next.run = { ...next.run, status: result.status };
  if (result.recovery) next.run = { ...next.run, recovery: result.recovery };
  if ((next.finished || completedEvent) && result.status) next.status = result.status;
  return next;
}

function reconcileRunMessages(live: LiveRun, chat: SavedChat | null): { parts: Message[]; showCurrent: boolean } {
  const visible = { parts: live.parts, showCurrent: true };
  const messageId = (value: unknown) => {
    const id = typeof value === 'number' ? value : typeof value === 'string' && /^[1-9]\d*$/.test(value) ? Number(value) : NaN;
    return Number.isSafeInteger(id) && id > 0 ? id : undefined;
  };
  const requestId = messageId(live.run.request_message_id);
  if (!chat || chat.id !== live.run.chat_id || requestId === undefined) return visible;
  const request = chat.messages.findIndex(message => messageId(message.id) === requestId && message.role === 'user');
  // A missing request row makes the position of a paginated transcript ambiguous.
  if (request < 0) return visible;
  const saved: Message[] = [];
  let previousId = requestId;
  for (const message of chat.messages.slice(request + 1)) {
    const id = messageId(message.id);
    if (id === undefined || id <= previousId || message.role === 'user' && message.interaction_run_id !== live.run.id) break;
    previousId = id;
    if (message.role === 'assistant' && (message.content || message.thinking)) saved.push(message);
  }
  const same = (left: Message, right: Message) => left.content === right.content && (left.thinking || '') === (right.thinking || '');
  let consumed = 0;
  while (consumed < live.parts.length && consumed < saved.length && same(live.parts[consumed], saved[consumed])) consumed++;
  const current = { role: 'assistant', content: live.text, thinking: live.thinking };
  return { parts: live.parts.slice(consumed), showCurrent: !(live.currentPersisted && consumed === live.parts.length && saved[consumed] && same(current, saved[consumed])) };
}

export default function App() {
  const [page, setPage] = useState<Page>('chat');
  const [showSetup, setShowSetup] = useState(false); const [showNotifications, setShowNotifications] = useState(false);
  const [settingsTab, setSettingsTab] = useState('general');
  const [sidebar, setSidebar] = useState(true); const [rightPanel, setRightPanel] = useState(false); const [panelTab, setPanelTab] = useState('activity');
  useEffect(() => { function showBrowser() { setRightPanel(true); setPanelTab('browser'); } window.addEventListener('forge:browser-open', showBrowser); return () => window.removeEventListener('forge:browser-open', showBrowser); }, []);
  const [contextOpen, setContextOpen] = useState(false);
  const [hud, setHud] = useState(false); const [peek, setPeek] = useState(false); const [modelOpen, setModelOpen] = useState(false);
  const [settings, setSettings] = useState<Settings>(DEFAULT_SETTINGS); const [projects, setProjects] = useState<Project[]>([]); const [chats, setChats] = useState<Chat[]>([]); const [models, setModels] = useState<Model[]>([]);
  const [projectId, setProjectId] = useState<string | null>(null); const [chatId, setChatId] = useState<string | null>(null); const [chat, setChat] = useState<SavedChat | null>(null);
  const [drafts, setDrafts] = useState<Record<string, string>>({}); const [attachments, setAttachments] = useState<Record<string, string[]>>({}); const [lives, setLives] = useState<Record<string, LiveRun>>({});
  const [pendingQuestionCount, setPendingQuestionCount] = useState(0);
  const [capabilities, setCapabilities] = useState<Record<string, unknown>>({}); const [connection, setConnection] = useState('Connecting'); const [notice, setNotice] = useState(''); const [busy, setBusy] = useState(false); const [search, setSearch] = useState('');
  const [recording, setRecording] = useState(false); const [transcribing, setTranscribing] = useState(false); const [dictationBusy, setDictationBusy] = useState(false); const [dictationSetupNeeded, setDictationSetupNeeded] = useState(false); const [captureBusy, setCaptureBusy] = useState(false); const [goalVersion, setGoalVersion] = useState(0); const [rename, setRename] = useState(false); const [renameValue, setRenameValue] = useState('');
  const [messagesLimit, setMessagesLimit] = useState(80); const [readyPlan, setReadyPlan] = useState<SavedPlan | null>(null); const [planPreview, setPlanPreview] = useState(false);
  const [authNeeded, setAuthNeeded] = useState(false); const [authInput, setAuthInput] = useState('');
  const booted = useRef(false); const liveRef = useRef(lives); const chatRef = useRef(chatId); const settingsQueue = useRef(Promise.resolve()); const settingsRevision = useRef(0); const polling = useRef(false); const chatScroll = useRef<HTMLDivElement>(null); const promptRef = useRef<HTMLTextAreaElement>(null); const fileInput = useRef<HTMLInputElement>(null); const shouldScroll = useRef(true); const dictationTarget = useRef(''); const dictationJob = useRef('');
  const recorder = useRef<MediaRecorder | null>(null); const recordingStream = useRef<MediaStream | null>(null); const browserAudio = useRef<Blob[]>([]); const recordingTimer = useRef<ReturnType<typeof setTimeout> | null>(null); const recordingCancelled = useRef(false);
  const chatNavigationVersion = useRef(0);
  const steerRequests = useRef(new Map<string, string>());
  liveRef.current = lives; chatRef.current = chatId;
  const draftKey = chatId || `new:${projectId || 'global'}`;
  const draft = drafts[draftKey] || ''; const images = attachments[draftKey] || [];
  const project = projects.find(p => p.id === projectId);
  const recentRuns = Object.values(lives).sort((a, b) => (Date.parse(b.run.created_at || '') || 0) - (Date.parse(a.run.created_at || '') || 0));
  const lastRun = recentRuns.find(l => l.run.chat_id === chatId);
  const live = recentRuns.find(l => l.run.chat_id === chatId && !l.finished) || (lastRun && ['paused', 'interrupted', 'failed'].includes(lastRun.run.status || '') ? lastRun : undefined);
  const visibleLive = live ? reconcileRunMessages(live, chat) : undefined;
  const speed = live?.speed ?? lastRun?.speed;
  const speedEstimated = live?.speedEstimated ?? lastRun?.speedEstimated;
  const running = Boolean(live && !live.finished);
  const speedLabel = speed !== undefined ? `${speed.toFixed(1)}${speedEstimated ? ' (estimated)' : ''}` : running ? '— (estimated)' : '—';
  const activeRuns = Object.values(lives).filter(l => !l.finished);
  const setDraft = useCallback((text: string) => setDrafts(prev => ({ ...prev, [draftKey]: text })), [draftKey]);

  const flash = useCallback((message: string) => { setNotice(message); setDictationSetupNeeded(false); }, []);
  async function refreshModels() {
    try { const result = await api<{ models: Model[] }>('models'); setModels(result.models || []); setConnection('Local engine'); }
    catch (e) { setConnection('Engine offline'); flash(errorText(e)); }
  }
  async function refreshWorkspace() {
    const results = await Promise.allSettled([api<{ projects: Project[] }>('projects'), api<{ chats: Chat[] }>('chats', { archived: 'all' })]);
    if (results[0].status === 'fulfilled') setProjects(results[0].value.projects || []);
    if (results[1].status === 'fulfilled') setChats(results[1].value.chats || []);
  }
  async function loadChat(id: string, limit = 80) {
    const version = ++chatNavigationVersion.current;
    try {
      const next = await api<SavedChat>('get_chat', { id, limit }); if (version !== chatNavigationVersion.current) return; setChat(next); setChatId(next.id); setProjectId(next.project_id); setMessagesLimit(limit); setPage('chat');
      if (next.model) setSettings(prev => ({ ...prev, model: next.model }));
      shouldScroll.current = true;
    } catch (e) { flash(errorText(e)); }
  }
  async function bootstrap() {
    if (booted.current) return; booted.current = true;
    try {
      const result = await api<Record<string, unknown>>('bootstrap');
      const saved = result.settings as Partial<Settings> || {};
      setSettings(prev => ({ ...prev, ...saved })); setProjects(listFrom<Project>(result, 'projects')); const savedChats = listFrom<Chat>(result, 'chats'); setChats(savedChats); setCapabilities(result.capabilities as Record<string, unknown> || {});
      const restored: Record<string, LiveRun> = {};
      for (const run of listFrom<Run>(result, 'runs')) if (ACTIVE.has(run.status || '') || ['paused', 'interrupted', 'failed', 'completed'].includes(run.status || '')) restored[run.id] = { run, text: '', thinking: '', parts: [], tools: [], cursor: 0, status: run.status || 'running', finished: !ACTIVE.has(run.status || '') };
      void api<{ first_run: boolean }>('setup_status').then(r => { if (r.first_run) setShowSetup(true); }).catch(() => {});
      setLives(restored); setConnection('Coordinator ready'); setAuthNeeded(false);
      if (savedChats[0]) await loadChat(savedChats[0].id);
      void refreshWorkspace();
      void refreshModels();
    } catch (e) { booted.current = false; setConnection('Disconnected'); flash(errorText(e)); if (/401|authenticate|unauthoriz|token|pair/i.test(errorText(e))) setAuthNeeded(true); }
  }
  useEffect(() => {
    const ready = () => { booted.current = false; void bootstrap(); };
    window.addEventListener('pywebviewready', ready);
    const timer = setTimeout(() => void bootstrap(), 300);
    return () => { clearTimeout(timer); window.removeEventListener('pywebviewready', ready); };
  }, []);
  useEffect(() => {
    let disposed = false;
    setReadyPlan(null);
    if (!lastRun || lastRun.run.mode !== 'plan' || lastRun.run.status !== 'completed') { setReadyPlan(null); return; }
    void api<{ plans: SavedPlan[] }>('plans', { chat_id: chatId }).then(result => { if (!disposed) setReadyPlan(result.plans.find(plan => plan.run_id === lastRun.run.id && plan.status === 'ready') || null); }).catch(error => { if (!disposed) flash(`Could not load the saved plan: ${errorText(error)}`); });
    return () => { disposed = true; };
  }, [chatId, lastRun?.run.id, lastRun?.run.status]);
  useEffect(() => {
    document.documentElement.dataset.theme = settings.theme;
    const media = window.matchMedia('(prefers-color-scheme: dark)');
    const update = () => document.documentElement.setAttribute('data-system-theme', media.matches ? 'dark' : 'light'); update(); media.addEventListener('change', update);
    return () => media.removeEventListener('change', update);
  }, [settings.theme]);
  useEffect(() => {
    if (shouldScroll.current && chatScroll.current) chatScroll.current.scrollTop = chatScroll.current.scrollHeight;
  }, [chat?.messages.length, live?.text, live?.thinking, live?.parts.length]);
  useEffect(() => {
    const tick = async () => {
      if (polling.current) return; const current = Object.values(liveRef.current).filter(l => !l.finished); if (!current.length) return;
      polling.current = true;
      try {
        const batches = await Promise.allSettled(current.map(async item => ({ id: item.run.id, result: await api<{ events: RunEvent[]; next_cursor?: number; status?: string; finished?: boolean; chat_id?: string }>('poll', { id: item.run.id, after: item.cursor }) })));
        const finished: string[] = [];
        setLives(prev => {
          const next = { ...prev };
          for (const batch of batches) if (batch.status === 'fulfilled') {
            const { id, result } = batch.value; if (!next[id]) continue; next[id] = applyEvents(next[id], result.events || [], result);
            if (result.finished && !prev[id].finished) finished.push(result.chat_id || next[id].run.chat_id || '');
          }
          return next;
        });
        // Human input events are emitted after their chat rows commit, so they can
        // be shown immediately while the same run continues its next round.
        for (const batch of batches) if (batch.status === 'fulfilled' && (batch.value.result.finished || batch.value.result.events?.some(event => event.type === 'question_answered' && Boolean(event.message_id) || event.type === 'steer' && (event.state || event.status) === 'applied'))) {
          const id = batch.value.result.chat_id || liveRef.current[batch.value.id]?.run.chat_id;
          if (id && id === chatRef.current) { const version = chatNavigationVersion.current; const next = await api<SavedChat>('get_chat', { id, limit: messagesLimit }); if (id === chatRef.current && version === chatNavigationVersion.current) setChat(next); }
          void refreshWorkspace(); setGoalVersion(v => v + 1);
        }
      } catch (e) { flash(`Reconnecting: ${errorText(e)}`); }
      finally { polling.current = false; }
    };
    const timer = setInterval(() => void tick(), 450); return () => clearInterval(timer);
  }, [messagesLimit]);
  useEffect(() => {
    let fetching = false;
    const timer = setInterval(async () => {
      if (!booted.current || fetching) return; fetching = true;
      try { const result = await api<{ runs: Run[] }>('runs'); const active = result.runs.filter(run => ACTIVE.has(run.status || '') && (!liveRef.current[run.id] || liveRef.current[run.id].finished));
        if (active.length) { setLives(prev => { const next = { ...prev }; for (const run of active) { const existing = next[run.id]; if (!existing) next[run.id] = { run, text: '', thinking: '', parts: [], tools: [], cursor: 0, status: run.status || 'Starting', finished: false }; else if (existing.finished) next[run.id] = { ...existing, run: { ...existing.run, ...run }, status: run.status || 'Resuming', finished: false }; } return next; }); void refreshWorkspace(); }
      } catch { /* Polling the current run retains reconnect state. */ }
      finally { fetching = false; }
    }, 3500); return () => clearInterval(timer);
  }, []);
  async function changeSettings(delta: Partial<Settings>) {
    const revision = ++settingsRevision.current;
    setSettings(prev => ({ ...prev, ...delta }));
    settingsQueue.current = settingsQueue.current.catch(() => {}).then(async () => {
      try { if ('startup' in delta) await api('startup', { enabled: delta.startup }); const saved = await api<Settings>('settings', delta); if (revision === settingsRevision.current) setSettings(prev => ({ ...prev, ...saved })); }
      catch (e) { flash(`Could not save preferences: ${errorText(e)}`); }
    });
    await settingsQueue.current;
  }
  async function switchMode(mode: boolean, expand = false) {
    try { if (isNative()) await native('mode', mode ? 'hud' : 'full', expand); setHud(mode); setPeek(expand); if (mode) setPage('chat'); }
    catch (e) { flash(errorText(e)); }
  }
  async function applyRecommendation(id: string) {
    const revision = ++settingsRevision.current;
    settingsQueue.current = settingsQueue.current.catch(() => {}).then(async () => {
      const result = await api<Partial<Settings> & { settings?: Settings }>('performance_apply', { id });
      if (revision === settingsRevision.current) setSettings(prev => ({ ...prev, ...(result.settings || result) }));
      await refreshModels();
    });
    await settingsQueue.current;
  }
  useEffect(() => {
    const keys = (e: KeyboardEvent) => {
      if (e.ctrlKey && e.shiftKey && e.key.toLowerCase() === 'h') { e.preventDefault(); void switchMode(!hud); }
      if (e.ctrlKey && e.key.toLowerCase() === 'n') { e.preventDefault(); newChat(); }
      if (e.key === 'Escape') { setModelOpen(false); if (running && e.shiftKey) void controlRun('cancel'); }
    }; window.addEventListener('keydown', keys); return () => window.removeEventListener('keydown', keys);
  }, [hud, running, live]);
  function newChat() { chatNavigationVersion.current++; chatRef.current = null; setChatId(null); setChat(null); setMessagesLimit(80); setReadyPlan(null); setPage('chat'); promptRef.current?.focus(); }
  function chooseProject(id: string | null) { chatNavigationVersion.current++; chatRef.current = null; setProjectId(id); setChatId(null); setChat(null); setPage('chat'); }
  async function chatChanged(id: string, change: ChatChange) {
    await refreshWorkspace();
    if (change === 'delete') { setLives(prev => Object.fromEntries(Object.entries(prev).filter(([, item]) => item.run.chat_id !== id))); setDrafts(prev => { const next = { ...prev }; delete next[id]; return next; }); setAttachments(prev => { const next = { ...prev }; delete next[id]; return next; }); }
    if (id === chatRef.current) { if (change === 'delete' || change === 'archive') newChat(); else await loadChat(id); }
    flash(change === 'delete' ? 'Conversation deleted.' : change === 'archive' ? 'Conversation archived.' : change === 'restore' ? 'Conversation restored.' : change === 'move' ? 'Conversation moved.' : 'Conversation renamed.');
  }
  async function projectRemoved(id: string) { await refreshWorkspace(); if (projectId === id) { setProjectId(null); if (chatId) await loadChat(chatId); } flash('Project registration removed. Its folder was kept.'); }
  async function registerProject() { try { if (isNative()) { const result = await native<Project & { cancelled?: boolean }>('choose_project'); if (!result.cancelled) { await refreshWorkspace(); chooseProject(result.id); } } else setPage('projects'); } catch (e) { flash(errorText(e)); } }
  function trackRun(run: Run) {
    chatNavigationVersion.current++;
    setLives(prev => ({ ...prev, [run.id]: { run: { ...run, created_at: run.created_at || new Date().toISOString(), model: run.model || run.settings?.model || settings.model }, text: '', thinking: '', parts: [], tools: [], cursor: 0, status: 'Loading model / waiting for engine…', finished: false } }));
    if (run.chat_id) { setChatId(run.chat_id); if (run.chat_id !== chatId) setChat({ id: run.chat_id, project_id: projectId, model: settings.model, title: draft.slice(0, 60), messages: [] }); }
  }
  async function send(override?: string) {
    const text = (override || draft).trim(); if (!text || busy) return;
    if (running && !/^\/(?:pause|status|help|project|model|agents|worktree|skill|mcp|schedule|new)(?:\s|$)/i.test(text)) { flash('Pause this run before starting another workflow. Use Steer to guide its current task.'); return; }
    setBusy(true); setNotice('');
    const user: Message = { role: 'user', content: text, images: images.length ? images : undefined };
    try {
      await settingsQueue.current;
      const payload = { ...settings, model: settings.model, text, project_id: projectId, chat_id: chatId, images, permission_profile: settings.permission_profile };
      const result = await api<Run & { navigate?: string; message?: string; goal?: unknown; mode?: string; chat?: Chat }>(text.startsWith('/') ? 'command' : 'start_chat', payload);
      if (result.navigate) {
        const target = result.navigate.toLowerCase();
        if (target === 'models' || target === 'mcp' || target === 'skills' || target === 'worktrees') { setSettingsTab(target === 'models' ? 'models' : target === 'mcp' ? 'mcp' : 'integrations'); setPage(target === 'worktrees' ? 'projects' : 'settings'); }
        else if (target === 'new') newChat(); else if (target === 'chat' && result.chat) await loadChat(result.chat.id); else if (target === 'goals') { setRightPanel(true); setPanelTab('goals'); setPage('chat'); } else if (navItems.some(n => n.page === target) || target === 'settings') setPage(target as Page);
      }
      if (result.id) {
        trackRun({ ...result, mode: text.startsWith('/plan ') ? 'plan' : result.mode, request: text.startsWith('/plan ') ? text.slice(6).trim() : text }); setChat(prev => prev ? { ...prev, messages: [...prev.messages, user] } : { id: result.chat_id || '', project_id: projectId, title: text.slice(0, 60), model: settings.model, messages: [user] });
        setReadyPlan(null); shouldScroll.current = true;
        if (/^\/(?:todo|to-do)(?:\s|$)/i.test(text)) { setRightPanel(true); setPanelTab('goals'); setPage('chat'); }
      }
      if (result.message) flash(result.message);
      setDrafts(prev => ({ ...prev, [draftKey]: '' })); setAttachments(prev => ({ ...prev, [draftKey]: [] }));
      setGoalVersion(v => v + 1); void refreshWorkspace();
    } catch (e) { flash(errorText(e)); }
    finally { setBusy(false); promptRef.current?.focus(); }
  }
  async function buildPlan() {
    if (!readyPlan || busy || running) return; setBusy(true); setNotice('');
    try { await settingsQueue.current; const run = await api<Run>('plan_build', { run_id: readyPlan.run_id }); trackRun(run); setReadyPlan(null); setPlanPreview(false); setRightPanel(true); setPanelTab('goals'); setGoalVersion(value => value + 1); await refreshWorkspace(); }
    catch (error) { flash(errorText(error)); } finally { setBusy(false); }
  }
  async function steer() {
    const text = draft.trim(); const runId = live?.run.id;
    if (!text || !runId || !running || busy) return;
    // Slash actions remain commands; they must never become ambiguous model instructions.
    if (text.startsWith('/')) { await send(); return; }
    const key = draftKey; const submitted = draft;
    const requestKey = `${runId}\0${text}`;
    const clientId = steerRequests.current.get(requestKey) || crypto.randomUUID();
    steerRequests.current.set(requestKey, clientId);
    setBusy(true); setNotice('');
    try {
      await api('run_steer', { run_id: runId, text, client_id: clientId });
      steerRequests.current.delete(requestKey);
      // A newer draft, or another chat's draft, belongs to the user and stays intact.
      setDrafts(prev => prev[key] === submitted ? { ...prev, [key]: '' } : prev);
      flash('Direction queued. Forge will apply it before its next model or tool step.');
    } catch (error) { flash(errorText(error)); }
    finally { setBusy(false); promptRef.current?.focus(); }
  }
  async function controlRun(action: 'pause' | 'resume' | 'cancel', id = live?.run.id) {
    if (!id) return; try {
      const result = await api<Run>(action, { id });
      if (action === 'resume') setLives(prev => ({ ...prev, [id]: { ...prev[id], finished: false, run: { ...prev[id].run, status: 'running', ...result }, status: 'Resuming' } }));
      else flash(action === 'pause' ? 'Pausing after the current safe step…' : 'Stopping…');
    } catch (e) { flash(errorText(e)); }
  }
  async function decide(approval: Approval, allowed: boolean) {
    try { await api('approve', { ...approval, allowed }); setLives(prev => ({ ...prev, [approval.job_id]: { ...prev[approval.job_id], approval: undefined, status: allowed ? 'Continuing' : 'Action denied' } })); } catch (e) { flash(errorText(e)); }
  }
  async function capture() {
    setCaptureBusy(true); try { const result = isNative() ? await native<{ image: string }>('screenshot') : await api<{ image: string }>('screenshot'); setAttachments(prev => ({ ...prev, [draftKey]: [...(prev[draftKey] || []), result.image] })); } catch (e) { flash(errorText(e)); } finally { setCaptureBusy(false); }
  }
  async function dictate() {
    if (dictationBusy) return; setDictationBusy(true); setDictationSetupNeeded(false);
    try {
      if (transcribing) { recordingCancelled.current = true; await api('dictation_cancel'); setTranscribing(false); dictationJob.current = ''; flash('Dictation cancelled.'); return; }
      if (!isNative()) {
        if (recording && recorder.current) { dictationJob.current = 'pending-upload'; recorder.current.stop(); setRecording(false); setTranscribing(true); return; }
        if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === 'undefined') throw new Error('Microphone recording requires a supported browser on localhost or HTTPS.');
        dictationTarget.current = draftKey; dictationJob.current = ''; recordingCancelled.current = false;
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true }); recordingStream.current = stream; browserAudio.current = [];
        const capture = new MediaRecorder(stream); recorder.current = capture;
        capture.ondataavailable = event => { if (event.data.size) browserAudio.current.push(event.data); };
        capture.onstop = async () => {
          stream.getTracks().forEach(track => track.stop()); recordingStream.current = null; recorder.current = null;
          if (recordingTimer.current) clearTimeout(recordingTimer.current);
          const chunks = browserAudio.current; browserAudio.current = [];
          if (recordingCancelled.current) return;
          try { const audio = new Blob(chunks, { type: capture.mimeType }); if (audio.size > 6_000_000) throw new Error('Dictation audio exceeds 6 MB. Try a shorter recording.');
            const encoded = await new Promise<string>((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(String(reader.result).split(',')[1]); reader.onerror = reject; reader.readAsDataURL(audio); });
            if (recordingCancelled.current) return;
            const result = await api<{ id: string }>('dictation_upload', { audio: encoded });
            if (recordingCancelled.current) { await api('dictation_cancel'); return; }
            dictationJob.current = result.id;
          } catch (e) { setTranscribing(false); flash(errorText(e)); }
          finally { chunks.length = 0; }
        };
        capture.start(1000); setRecording(true);
        recordingTimer.current = setTimeout(() => { if (capture.state === 'recording') { dictationJob.current = 'pending-upload'; capture.stop(); setRecording(false); setTranscribing(true); } }, 120000);
        return;
      }
      if (!recording) { dictationTarget.current = draftKey; dictationJob.current = ''; }
      const result = await api<{ state: string; id?: string; text?: string; message?: string; setup_required?: boolean }>(recording ? 'dictation_stop' : 'dictation_start');
      if (!['recording', 'transcribing', 'done', 'cancelled'].includes(result.state)) { setDictationSetupNeeded(Boolean(result.setup_required)); throw new Error(result.message || 'Dictation could not start. Check Settings → Dictation.'); }
      setNotice('');
      setRecording(result.state === 'recording'); setTranscribing(result.state === 'transcribing'); if (result.id) dictationJob.current = result.id;
      if (result.state === 'done') { const text = result.text?.trim(); if (text) setDrafts(prev => ({ ...prev, [dictationTarget.current]: `${prev[dictationTarget.current] || ''}${prev[dictationTarget.current] ? ' ' : ''}${text}` })); flash(text ? 'Transcript added to your draft.' : 'No speech detected. Try again and check your microphone.'); }
    } catch (e) { setRecording(false); setTranscribing(false); dictationJob.current = ''; flash(errorText(e)); setDictationSetupNeeded(true); }
    finally { setDictationBusy(false); }
  }
  useEffect(() => {
    if ((!recording && !transcribing) || (recording && !isNative())) return;
    let fetching = false;
    const timer = setInterval(async () => {
      if (fetching) return; fetching = true;
      try { const result = await api<{ state: string; id?: string; text?: string; limit_reached?: boolean; message?: string }>('dictation_status');
        if (result.state === 'error') throw new Error(result.message || 'Dictation failed. Check your microphone and installed model.');
        if (result.state === 'done' && (!dictationJob.current || result.id === dictationJob.current)) {
          const target = dictationTarget.current;
          if (result.text) setDrafts(prev => ({ ...prev, [target]: `${prev[target] || ''}${prev[target] ? ' ' : ''}${result.text}` }));
          setTranscribing(false); setRecording(false); dictationJob.current = ''; flash(result.text?.trim() ? 'Transcript added to your draft.' : 'No speech detected. Try again and check your microphone.');
        } else if (result.state === 'cancelled') { setTranscribing(false); setRecording(false); }
        else if (result.limit_reached && result.state === 'recording') { const next = await api<{ id: string }>('dictation_stop'); dictationJob.current = next.id; setRecording(false); setTranscribing(true); }
      } catch (e) { setRecording(false); setTranscribing(false); flash(errorText(e)); setDictationSetupNeeded(true); }
      finally { fetching = false; }
    }, 600); return () => clearInterval(timer);
  }, [recording, transcribing]);
  useEffect(() => () => { recordingCancelled.current = true; if (recordingTimer.current) clearTimeout(recordingTimer.current); if (recorder.current?.state === 'recording') recorder.current.stop(); recordingStream.current?.getTracks().forEach(track => track.stop()); browserAudio.current = []; }, []);
  async function attachFiles(files: FileList | null) {
    if (!files) return;
    for (const file of Array.from(files).slice(0, 4)) {
      if (!file.type.startsWith('image/')) { flash('Attach an image here; connect a project folder for source files.'); continue; }
      if (file.size > 5_000_000) { flash('Choose an image below 5 MB.'); continue; }
      const data = await new Promise<string>((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(String(reader.result).split(',')[1]); reader.onerror = reject; reader.readAsDataURL(file); });
      setAttachments(prev => ({ ...prev, [draftKey]: [...(prev[draftKey] || []), data].slice(0, 4) }));
    }
    if (fileInput.current) fileInput.current.value = '';
  }
  const pendingApprovals = activeRuns.filter(r => r.approval).map(r => r.approval!);
  useEffect(() => { if (hud && !peek && (modelOpen || pendingApprovals.length > 0 || pendingQuestionCount > 0 || images.length > 0)) void switchMode(true, true); }, [hud, peek, modelOpen, pendingApprovals.length, pendingQuestionCount, images.length]);
  const composer = <div className="composer-area">
    <QuestionPanel chatId={chatId} active={running} notify={flash} onPending={setPendingQuestionCount} />
    {pendingApprovals.length > 0 && <div className="approval-stack">{pendingApprovals.map(approval => <div className="approval-card" key={approval.approval_id}><div><ShieldCheck size={16} /><strong>Approval needed</strong><span>{approval.name}</span></div><pre>{approval.command || JSON.stringify(approval.arguments || {}, null, 2)}</pre><footer><button className="subtle" onClick={() => void decide(approval, false)}>Deny</button><button className="primary" onClick={() => void decide(approval, true)}>Allow once</button></footer></div>)}</div>}
    {notice && (!hud || peek) && <div className="notice" role="status"><span>{notice}</span>{dictationSetupNeeded && <button className="text-button" onClick={() => { setSettingsTab('dictation'); setPage('settings'); setDictationSetupNeeded(false); if (hud) void switchMode(false); }}>Open Dictation settings</button>}<IconButton label="Dismiss notice" onClick={() => setNotice('')}><X size={13} /></IconButton></div>}
    <div className="composer">
      {draft.startsWith('/') && !draft.includes(' ') && <SlashPalette query={draft} select={text => { setDraft(text); promptRef.current?.focus(); }} />}
      {images.length > 0 && <div className="attachment-strip">{images.map((image, index) => <div key={index}><img src={`data:image/jpeg;base64,${image}`} alt={`Attachment ${index + 1}`} /><IconButton label={`Remove attachment ${index + 1}`} onClick={() => setAttachments(prev => ({ ...prev, [draftKey]: images.filter((_, i) => i !== index) }))}><X size={12} /></IconButton></div>)}</div>}
      <textarea ref={promptRef} aria-label="Message Forge" placeholder={recording ? 'Listening… click the microphone to finish' : running ? 'Guide the current run…' : 'Ask Forge, or type / for commands'} value={draft} onChange={e => setDraft(e.target.value)} rows={hud ? 1 : 2} maxLength={72000} onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); if (running) void steer(); else void send(); } }} />
      <div className="composer-controls"><div className="composer-left"><IconButton label="Attach image" disabled={running} onClick={() => fileInput.current?.click()}><Plus size={19} /></IconButton><IconButton label={captureBusy ? 'Capturing screen' : 'Attach screenshot'} onClick={() => void capture()} disabled={running || captureBusy || !isNative()}><Maximize2 size={16} /></IconButton><ModelPicker models={models} value={settings.model} providerId={String(settings.provider_id || 'ollama')} select={name => void changeSettings({ model: name })} open={modelOpen} setOpen={setModelOpen} refresh={() => void refreshModels()} /><button className="permission-control" title="Permission profile" onClick={() => { setPage('settings'); setSettingsTab('permissions'); if (hud) void switchMode(false); }}><ShieldCheck size={13} /><span>{settings.permission_profile === 'full_access' ? 'Full access' : settings.permission_profile === 'deny_access' ? 'Chat only' : 'Ask'}</span></button></div><div className="composer-right"><ContextControl settings={settings} onChange={changeSettings} open={contextOpen} setOpen={value => { setContextOpen(value); if (value && hud && !peek) void switchMode(true, true); }} /><IconButton label={dictationBusy ? 'Preparing dictation' : transcribing ? 'Cancel transcription' : recording ? 'Stop dictation' : 'Start dictation'} active={recording} disabled={dictationBusy} onClick={() => void dictate()}>{dictationBusy ? <LoaderCircle size={17} className="spin" /> : recording ? <AudioLines size={17} className="pulse" /> : <Mic size={17} />}</IconButton><>{running && <button className="steer-button" title="Guide the current run (Enter)" disabled={busy || !draft.trim()} onClick={() => void steer()}><Waypoints size={14} /><span>{draft.startsWith('/') ? 'Command' : 'Steer'}</span></button>}<button className="send-button" aria-label={running ? 'Stop generation' : 'Send message'} title={running ? 'Stop (Shift+Escape)' : 'Send (Enter)'} disabled={!running && (busy || !draft.trim() || (!settings.model && !draft.startsWith('/')) || !booted.current)} onClick={() => running ? void controlRun('cancel') : void send()}>{running ? <Square size={14} fill="currentColor" /> : <ArrowUp size={18} />}</button></></div></div>
    </div>
    <div className="composer-status"><span className={`composer-connection ${running ? 'status-running' : ''}`}><i />{dictationBusy ? 'Loading local dictation…' : live?.status || connection}</span><span className="composer-metrics"><span>{tokenLabel(settings.context)} context</span><span aria-hidden="true">·</span><span className="metric-speed" aria-label="Generation speed in tokens per second">Tokens/s: {speedLabel}</span></span></div>
  </div>;

  return <div className={`app ${hud ? 'hud' : ''} ${peek ? 'peek' : ''} ${!sidebar ? 'sidebar-collapsed' : ''}`}>
    <input ref={fileInput} type="file" hidden accept="image/*" multiple onChange={e => void attachFiles(e.target.files)} />
    <Suspense fallback={null}>{showSetup && <SetupWizard settings={settings} models={models} onChange={changeSettings} refreshModels={refreshModels} close={() => setShowSetup(false)} openSettings={tab => { setShowSetup(false); setSettingsTab(tab); setPage('settings'); }} />}{showNotifications && <NotificationCenter close={() => setShowNotifications(false)} openRun={id => { const item = liveRef.current[id]; if (item?.run.chat_id) void loadChat(item.run.chat_id); else void api<{ runs: Run[] }>('runs').then(r => { const run = r.runs.find(v => v.id === id); if (run?.chat_id) void loadChat(run.chat_id); }); setShowNotifications(false); }} />}</Suspense>
    {!hud && sidebar && <WorkspaceSidebar page={page} projects={projects} chats={chats} projectId={projectId} chatId={chatId} search={search} onSearch={setSearch} activeChatIds={activeRuns.map(item => item.run.chat_id || '').filter(Boolean)} navItems={navItems} onPage={setPage} onNew={newChat} onCollapse={() => setSidebar(false)} onConnect={() => void registerProject()} onProject={chooseProject} onChat={id => void loadChat(id)} onChatChanged={chatChanged} onProjectRemoved={projectRemoved} />}
    <div className="main-column">
      <header className="workspace-header"><div className="header-left">{!sidebar && !hud && <IconButton label="Open sidebar" onClick={() => setSidebar(true)}><PanelLeftOpen size={17} /></IconButton>}{hud ? <img className="hud-logo" src="./forge.svg" alt="Forge" /> : <><button className="header-project" onClick={() => setPage('projects')}><Folder size={14} /><span>{project?.name || 'Personal workspace'}</span><ChevronDown size={12} /></button><ChevronRight size={12} /><button className="header-title" title="Rename chat" onClick={() => { if (chatId) { setRenameValue(chat?.title || ''); setRename(true); } }}>{page === 'chat' ? chat?.title || 'New chat' : page === 'scheduled' ? 'Scheduled' : page.charAt(0).toUpperCase() + page.slice(1)}</button></>}{project?.branch && <span className="branch-badge"><GitBranch size={12} />{project.branch}</span>}</div><div className="header-right">{hud && notice && !peek && <IconButton label="Show notification" onClick={() => void switchMode(true, true)}><Info size={15} /></IconButton>}{!hud && <IconButton label="Notifications" onClick={() => setShowNotifications(true)}><Bell size={17} /></IconButton>}{hud && <button className="hud-reply-toggle text-button" aria-expanded={peek} onClick={() => void switchMode(true, !peek)}>{peek ? 'Hide reply' : 'Show reply'}</button>}{!hud && isNative() && <IconButton label="Open Forge browser" onClick={() => { setRightPanel(true); setPanelTab('browser'); }}><Globe size={17} /></IconButton>}{running && <IconButton label="Pause run" onClick={() => void controlRun('pause')}><Pause size={15} /></IconButton>}{live && ['paused', 'interrupted', 'failed'].includes(live.run.status || '') && <button className="resume-button" onClick={() => void controlRun('resume')}><Play size={13} />Resume</button>}<IconButton label={hud ? 'Full workspace' : 'Minimal HUD'} onClick={() => void switchMode(!hud)}>{hud ? <Expand size={17} /> : <Minimize2 size={17} />}</IconButton>{!hud && <IconButton label={rightPanel ? 'Close workspace panel' : 'Open workspace panel'} active={rightPanel} onClick={() => setRightPanel(!rightPanel)}>{rightPanel ? <PanelRightClose size={17} /> : <PanelRightOpen size={17} />}</IconButton>}</div></header>
      <div className="workspace-body"><main className={`content ${page !== 'chat' ? 'page-content' : 'chat-content'}`}>
        {page === 'chat' ? <><div className="chat-scroll" ref={chatScroll} onScroll={() => { const el = chatScroll.current; if (el) shouldScroll.current = el.scrollHeight - el.scrollTop - el.clientHeight < 120; }}><div className="chat-width">{!chat?.messages?.length && !live?.text && !live?.thinking ? <div className="welcome"><div className="welcome-mark"><img src="./forge.svg" alt="" /></div><h1>What are we building?</h1><p>Your models. Your projects. Your workspace.</p><div className="welcome-suggestions"><button onClick={() => { setDraft('/plan '); promptRef.current?.focus(); }}><Command size={16} /><span>Plan a change<small>Explore before building</small></span></button><button onClick={() => { setDraft('/goal '); promptRef.current?.focus(); }}><Check size={16} /><span>Start a goal<small>Keep progress through long tasks</small></span></button><button onClick={() => void registerProject()}><FolderPlus size={16} /><span>Open a project<small>Connect a local folder</small></span></button></div></div> : <>{(chat?.total_messages || 0) > (chat?.messages?.length || 0) && <button className="load-earlier" onClick={() => chatId && void loadChat(chatId, messagesLimit + 80)}>Load earlier messages</button>}{chat?.summary && <div className="compaction-note"><Layers3 size={13} />Earlier context compacted · Original messages saved</div>}{chat?.messages?.map((message, index) => <MessageView key={message.id || `${chat.id}-${index}`} message={message} showThinking={settings.thinking} />)}{live && !live.finished && <>{visibleLive?.parts.map((message, i) => <MessageView key={`part-${i}`} message={message} showThinking={settings.thinking} />)}{live.tools.slice(-12).map((event, i) => <ToolCard key={`tool-${i}`} event={event} />)}{visibleLive?.showCurrent && (live.text || live.thinking) && <MessageView message={{ role: 'assistant', content: live.text, thinking: live.thinking }} streaming showThinking={settings.thinking} />}</>}{readyPlan && !running && <div className="plan-actions"><div><Badge>Plan ready</Badge><span>{readyPlan.tasks.length} steps saved</span><small className="goal-build-hint">Build starts a tracked goal</small><button className="text-button" onClick={() => setPlanPreview(true)}>Review plan</button></div><button className="primary" disabled={busy} onClick={() => void buildPlan()}><Play size={13} />Build</button></div>}</>}</div></div>{composer}</> : <div className="page-scroll">{page === 'projects' && <ProjectsPage projects={projects} selected={projectId} open={chooseProject} refresh={refreshWorkspace} notify={flash} />}{page === 'spaces' && <SpacesPage projects={projects} notify={flash} />}{page === 'scheduled' && <SchedulesPage projects={projects} notify={flash} onRun={trackRun} />}{page === 'agents' && <AgentsPage models={models} projects={projects} projectId={projectId} settings={settings} notify={flash} onRun={trackRun} />}{page === 'plugins' && <PluginsPage notify={flash} projectId={projectId} />}{page === 'usage' && <UsagePage projects={projects} models={models} timezone={settings.timezone} />}{page === 'memory' && <Suspense fallback={<p className="muted">Loading memory…</p>}><MemoryPage projects={projects} settings={settings} onChange={changeSettings} notify={flash} /></Suspense>}{page === 'settings' && <SettingsPage settings={settings} onChange={changeSettings} applyRecommendation={applyRecommendation} models={models} refreshModels={refreshModels} tab={settingsTab} setTab={setSettingsTab} capabilities={capabilities} openSetup={() => setShowSetup(true)} notify={flash} />}</div>}
      </main>{rightPanel && !hud && <aside className={`right-panel ${panelTab === 'browser' ? 'with-browser' : ''}`}><nav><button className={panelTab === 'browser' ? 'active' : ''} onClick={() => setPanelTab('browser')}>Browser</button><button className={panelTab === 'activity' ? 'active' : ''} onClick={() => setPanelTab('activity')}>Activity</button><button className={panelTab === 'goals' ? 'active' : ''} onClick={() => setPanelTab('goals')}>Goals</button><button className={panelTab === 'files' ? 'active' : ''} onClick={() => setPanelTab('files')}>Files</button></nav><div className={`right-scroll ${panelTab === 'browser' ? 'browser-scroll' : ''}`}><Suspense fallback={<p className="muted">Loading browser…</p>}>{panelTab === 'browser' && <BrowserPanel />}</Suspense>{panelTab === 'activity' && <>{activeRuns.length === 0 && <Empty icon={<Activity size={22} />} title="All quiet" text="Agent steps and command output appear here." />}{recentRuns.slice(0,12).map(item => <div className="run-card" key={item.run.id}><header><Bot size={14} /><strong>{item.run.parent_id ? 'Sub-agent' : 'Main agent'}</strong><Badge>{item.run.status || item.status}</Badge></header><small>{friendlyModel(item.run.model || settings.model)}</small><p>{item.status}</p>{item.finished && ['paused', 'interrupted', 'failed'].includes(item.run.status || '') && <RecoveryPanel runId={item.run.id} recovery={item.run.recovery} notify={flash} />}{item.tools.slice(-6).map((event, i) => <ToolCard key={i} event={event} />)}{item.finished && ['paused', 'interrupted', 'failed'].includes(item.run.status || '') && <button className="secondary" onClick={() => void controlRun('resume', item.run.id)}><Play size={13} />Resume</button>}</div>)}</>}{panelTab === 'goals' && <GoalsPanel projectId={projectId} chats={chats} version={goalVersion} notify={flash} onRun={trackRun} />}{panelTab === 'files' && <FilePanel projectId={projectId} notify={flash} />}</div></aside>}</div>
      {!hud && page !== 'chat' && notice && <div className="global-notice" role="status">{notice}<IconButton label="Dismiss notice" onClick={() => setNotice('')}><X size={14} /></IconButton></div>}
    </div>
    {planPreview && readyPlan && <Modal title="Implementation plan" wide close={() => setPlanPreview(false)}><p className="muted">{readyPlan.request}</p><Markdown text={readyPlan.markdown} />{readyPlan.path && <small className="plan-file-path">{readyPlan.path}</small>}<footer><button className="subtle" onClick={() => setPlanPreview(false)}>Close</button><button className="primary" disabled={busy || running} onClick={() => void buildPlan()}><Play size={14} />Build this plan</button></footer></Modal>}
    {rename && <Modal title="Rename conversation" close={() => setRename(false)}><form onSubmit={e => { e.preventDefault(); void api('rename_chat', { id: chatId, title: renameValue }).then(() => { setChat(prev => prev ? { ...prev, title: renameValue } : prev); void refreshWorkspace(); setRename(false); }).catch(e => flash(errorText(e))); }}><input autoFocus aria-label="Conversation title" value={renameValue} maxLength={200} onChange={e => setRenameValue(e.target.value)} /><footer><button className="primary" disabled={!renameValue.trim()}>Save</button></footer></form></Modal>}
    {authNeeded && <Modal title="Connect to your coordinator" close={() => setAuthNeeded(false)}><p className="muted">Generate a pairing code in the Forge desktop app under Settings → General, then enter it here.</p><form onSubmit={e => { e.preventDefault(); void api('pair', { code: authInput }).then(() => { setAuthInput(''); booted.current = false; void bootstrap(); }).catch(e => flash(errorText(e))); }}><input type="text" autoComplete="off" autoFocus aria-label="Browser pairing code" value={authInput} onChange={e => setAuthInput(e.target.value)} /><footer><button className="primary">Connect</button></footer></form></Modal>}
  </div>;
}

function FilePanel({ projectId, notify }: { projectId: string | null; notify: (message: string) => void }) {
  const [path, setPath] = useState(''); const [files, setFiles] = useState<{ name: string; path?: string; is_dir?: boolean; type?: string }[]>([]); const [preview, setPreview] = useState(''); const [previewName, setPreviewName] = useState(''); const [error, setError] = useState('');
  useEffect(() => { setPath(''); setPreview(''); setPreviewName(''); setError(''); }, [projectId]);
  useEffect(() => { let disposed = false; setFiles([]); setPreview(''); if (!projectId) return; void api<{ entries?: typeof files; files?: typeof files }>('workspace_files', { project_id: projectId, path: path || '.' }).then(r => { if (disposed) return; setFiles(r.entries || r.files || []); setError(''); }).catch(e => { if (!disposed) setError(errorText(e)); }); return () => { disposed = true; }; }, [projectId, path]);
  async function open(file: typeof files[number]) { const name = file.path || `${path ? path + '/' : ''}${file.name}`; if (file.is_dir || file.type === 'directory') { setPath(name); return; } try { const result = await api<{ content: string }>('workspace_read', { project_id: projectId, path: name }); setPreview(result.content); setPreviewName(name); } catch (e) { notify(errorText(e)); } }
  if (!projectId) return <Empty icon={<Folder size={22} />} title="Connect a project" text="Browse files after selecting a project folder." />;
  return <div className="file-panel">{previewName ? <><button className="text-button" onClick={() => setPreviewName('')}>← Files</button><h3>{previewName}</h3><pre>{preview}</pre></> : <><div className="file-path"><button className="text-button" onClick={() => setPath('')}>Project</button><span>{path}</span></div>{path && <button className="file-row" onClick={() => setPath(path.split('/').slice(0, -1).join('/'))}><Folder size={14} />..</button>}{error && <div className="inline-error">{error}</div>}{files.map(file => <button className="file-row" key={file.name} onClick={() => void open(file)}>{file.is_dir || file.type === 'directory' ? <Folder size={14} /> : <FileCode2 size={14} />}<span>{file.name}</span></button>)}</>}</div>;
}

export { applyEvents, reconcileRunMessages };
