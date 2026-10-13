import { lazy, Suspense, useCallback, useEffect, useLayoutEffect, useRef, useState, type CSSProperties } from 'react';
import { Sparkles, Info, Bell, Brain, Activity, ArrowUp, AudioLines, Bot, Check, ChevronDown, ChevronRight, Clock3, Command, Expand, FileCode2, Folder, FolderPlus, GitBranch, Globe, Layers3, LoaderCircle, Maximize2, MessageSquare, Mic, Minimize2, PanelLeftClose, PanelLeftOpen, PanelRightClose, PanelRightOpen, Pause, Play, Plug, Plus, Search, Settings2, ShieldCheck, Square, Terminal, Waypoints, X } from 'lucide-react';
import { api, errorText, friendlyModel, isNative, listFrom, native, tokenLabel } from './api';
import { Badge, Empty, IconButton, Markdown, MessageView, ModelPicker, Modal, SlashPalette, ToolCard, slashCommands, ComposerModeChip } from './components';
import { AgentsPage, GoalsPanel, ProjectsPage, SchedulesPage, SpacesPage, UsagePage } from './pages';
import { loadSettingsPage, SettingsPageLoader } from './settingsLoader';
import { ContextControl } from './contextControl';
import { WorkspaceSidebar } from './workspaceSidebar';
import { QuestionPanel } from './questionPanel';
import { TaskContextPanel } from './taskContextPanel';
import { WorkspaceActivity, WorkspacePanel } from './workspacePanel';
import { usePersistentDraft, type DraftCommandMode } from './persistentDraft';
import { consumeWorkflowPrefix,isComposerMode,requestMode } from './composerModes';
import { installReadinessFence, updateReadiness, useReadinessParticipant, useUpdateReadiness, type ReadinessReason } from './updateReadiness';
import { UpdateHandoffSurface } from './updateHandoff';
import { TaskStatus } from './taskStatus';
import { SubmissionRecovery, type SubmissionScope } from './submissionRecovery';
import { appearance, ProjectIcon } from './appearance';
import { ResizeHandle, useWorkspaceLayout } from './workspaceLayout';
import { useWorkspaceVisibility } from './workspaceVisibility';
import { activityRunIds, ChatActivity, transcriptActivity } from './chatActivity';
import type { ChatChange } from './chatActions';
import { DEFAULT_SETTINGS } from './types';
import type { ThinkingReadIntent } from './thinkingDisclosure';
import type { Approval, Chat, Message, Model, Page, Project, Run, RunEvent, SavedChat, Settings } from './types';

const BuilderPage = lazy(() => import('./BuilderPage').then(m => ({ default: m.BuilderPage })));
const BrowserPanel = lazy(() => import('./browserPanel').then(m => ({ default: m.BrowserPanel })));
const SetupWizard = lazy(() => import('./setupWizard').then(m => ({ default: m.SetupWizard })));
const PluginsPage = lazy(() => import('./pluginsPage').then(m => ({ default: m.PluginsPage })));
const MemoryPage = lazy(() => import('./memoryPage').then(m => ({ default: m.MemoryPage })));
const NotificationCenter = lazy(() => import('./channelSettings').then(m => ({ default: m.NotificationCenter })));

interface LiveRun { run: Run; text: string; thinking: string; parts: Message[]; tools: RunEvent[]; cursor: number; status: string; finished: boolean; currentPersisted?: boolean; speed?: number; speedEstimated?: boolean; firstToken?: number; approval?: Approval; }
interface DocumentAttachment { id: string; name: string; }
interface PendingAttachment { id: string; name: string; state: 'processing' | 'error'; error?: string; }
interface SavedPlan { id: string; run_id: string; chat_id: string; project_id?: string; request: string; status: string; markdown: string; tasks: { text: string; status: string }[]; path?: string; }
const ACTIVE = new Set(['queued', 'running', 'compacting', 'reviewing', 'waiting_approval', 'awaiting_approval', 'waiting_question', 'awaiting_question', 'starting', 'resuming']);
const navItems: { page: Page; label: string; icon: typeof Folder }[] = [
  { page: 'builder', label: 'Builder', icon: FileCode2 }, { page: 'projects', label: 'Projects', icon: Folder }, { page: 'spaces', label: 'Spaces', icon: Layers3 }, { page: 'scheduled', label: 'Scheduled', icon: Clock3 }, { page: 'plugins', label: 'Library', icon: Plug }, { page: 'agents', label: 'Agents', icon: Bot }, { page: 'usage', label: 'Usage', icon: Activity }, { page: 'memory', label: 'Memory', icon: Brain },
];

function applyEvents(live: LiveRun, events: RunEvent[], result: { next_cursor?: number; status?: string; finished?: boolean; recovery?: string; progress_summary?: Run['progress_summary']; planner_assignment?: Run['planner_assignment'] }): LiveRun {
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
    else if (['skill','specialist','context_snapshot','tool_selection','progress_nudge','verification'].includes(event.type)) { next.tools.push(event); }
    else if (event.type === 'progress_summary' && event.summary) next.run = {...next.run, progress_summary:event.summary as Run['progress_summary']};
    else if (event.type === 'planner' && event.assignment) next.run = {...next.run, planner_assignment:event.assignment as Run['planner_assignment']};
    else if (event.type === 'approval') { next.approval = { job_id: live.run.id, approval_id: String(event.approval_id || event.id || ''), name: String(event.name || event.tool || 'Tool action'), arguments: event.arguments, command: event.command }; next.currentPersisted = true; next.status = 'Approval needed'; }
    else if (event.type === 'question') { next.currentPersisted = true; next.status = 'Waiting for your answers'; }
    else if (event.type === 'question_answered') next.status = 'Continuing with your answers';
    else if (event.type === 'goal_review' && event.review && typeof event.review === 'object') { next.run = { ...next.run, review: event.review as NonNullable<Run['review']> }; next.status = next.run.review?.status === 'reviewing' ? 'Independent goal review' : next.run.review?.status === 'complete' ? 'Completion verified' : next.run.review?.status === 'needs_changes' ? 'Applying reviewer feedback' : next.run.review?.status === 'disabled' ? 'Independent review off' : 'Goal review needs attention'; }
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
  if (result.progress_summary) next.run = {...next.run, progress_summary:result.progress_summary};
  if (result.planner_assignment) next.run = {...next.run, planner_assignment:result.planner_assignment};
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

type ThinkingReads = Map<string, { chatId: string; intent: ThinkingReadIntent }>;
function thinkingReadOwners(live: LiveRun, chat: SavedChat | null, reads: ThinkingReads, epoch: number, create: boolean, focusAuthority?: { revision: number }) {
  const saved = new Map<string, ThinkingReadIntent>();
  const parts: (ThinkingReadIntent | undefined)[] = [];
  const empty = { saved, parts, current: undefined as ThinkingReadIntent | undefined };
  const positiveId = (value: unknown) => {
    const id = typeof value === 'number' ? value : typeof value === 'string' && /^[1-9]\d*$/.test(value) ? Number(value) : NaN;
    return Number.isSafeInteger(id) && id > 0 ? id : undefined;
  };
  const requestId = positiveId(live.run.request_message_id);
  if (!chat || chat.id !== live.run.chat_id || !live.run.id || requestId === undefined) return empty;
  const anchor = chat.messages.findIndex(message => message.role === 'user' && positiveId(message.id) === requestId);
  if (anchor < 0) return empty;
  const owner = (turn: number) => {
    const key = JSON.stringify([epoch, chat.id, live.run.id, requestId, turn]);
    let record = reads.get(key);
    if (!record && create) {
      if (reads.size >= 256) reads.delete(reads.keys().next().value!);
      record = { chatId: chat.id, intent: { key, focusAuthority } }; reads.set(key, record);
    }
    return record?.intent;
  };
  for (let turn = 0; turn < live.parts.length; turn++) parts.push(live.parts[turn].thinking ? owner(turn) : undefined);
  const current = live.thinking ? owner(live.parts.length) : undefined;
  const turns = [...live.parts, { role: 'assistant', content: live.text, thinking: live.thinking }];
  let previous = requestId, turn = 0;
  for (const message of chat.messages.slice(anchor + 1)) {
    const id = positiveId(message.id);
    if (id === undefined || id <= previous || message.role === 'user' && message.interaction_run_id !== live.run.id) break;
    previous = id;
    if (message.role !== 'assistant' || !message.content && !message.thinking) continue;
    // Equality is only a sanity check inside an exact owned, ordered exchange.
    const source = turns[turn];
    if (message.interaction_run_id !== live.run.id || !source || source.content !== message.content || (source.thinking || '') !== (message.thinking || '')) break;
    const intent = turn < parts.length ? parts[turn] : current;
    if (intent) saved.set(String(id), intent);
    turn++;
  }
  return { saved, parts, current };
}

export default function App() {
  const [page, setPageState] = useState<Page>('chat');
  const thinkingReads = useRef<ThinkingReads>(new Map());
  const thinkingFocus = useRef({ revision: 0 });
  useEffect(() => {
    const changed = () => { thinkingFocus.current.revision++; };
    const focused = (event: FocusEvent) => { if (event.target !== document.body) changed(); };
    document.addEventListener('focusin', focused, true);
    document.addEventListener('pointerdown', changed, true);
    document.addEventListener('wheel', changed, {capture:true,passive:true});
    document.addEventListener('click', changed, true);
    document.addEventListener('keydown', changed, true);
    document.addEventListener('visibilitychange', changed);
    window.addEventListener('blur', changed);
    window.addEventListener('forge:navigate', changed);
    return () => {
      document.removeEventListener('focusin', focused, true);
      document.removeEventListener('pointerdown', changed, true);
      document.removeEventListener('wheel', changed, true);
      document.removeEventListener('click', changed, true);
      document.removeEventListener('keydown', changed, true);
      document.removeEventListener('visibilitychange', changed);
      window.removeEventListener('blur', changed);
      window.removeEventListener('forge:navigate', changed);
    };
  }, []);
  const navigation = useRef(0);
  const setPage = useCallback((next: Page) => { if(!updateReadiness.canEdit())return; const event = new CustomEvent('forge:navigate', {cancelable: true, detail: {page: next}}); if (!window.dispatchEvent(event)) return; navigation.current++; setPageState(next); }, []);
  const [showSetup, setShowSetup] = useState(false); const [showNotifications, setShowNotifications] = useState(false);
  const [settingsTab, setSettingsTab] = useState('general');
  const [compactViewport,setCompactViewport]=useState(()=>window.innerWidth<800), [smallSidebar,setSmallSidebar]=useState(false);
  const [rightPanel, setRightPanelState] = useState(false); const [panelTab, setPanelTabState] = useState('activity');const panelTouched=useRef(false);
  const setRightPanel=(value:boolean)=>{if(!updateReadiness.canEdit())return;panelTouched.current=true;setRightPanelState(value);};const setPanelTab=(value:string)=>{if(!updateReadiness.canEdit())return;panelTouched.current=true;setPanelTabState(value);};
  useEffect(()=>{const resize=()=>{setCompactViewport(innerWidth<800);if(innerWidth>=800)setSmallSidebar(false);};window.addEventListener('resize',resize);return()=>window.removeEventListener('resize',resize);},[]);
  useEffect(() => { function showBrowser() { setRightPanel(true); setPanelTab('browser'); } window.addEventListener('forge:browser-open', showBrowser); return () => window.removeEventListener('forge:browser-open', showBrowser); }, []);
  const [contextOpen, setContextOpen] = useState(false);
  const [hud, setHud] = useState(false); const [peek, setPeek] = useState(false); const [modelOpen, setModelOpen] = useState(false);
  useEffect(() => { function showPreview() { if (page !== 'builder') setPage('builder'); if (hud) void switchMode(false); } window.addEventListener('forge:preview-open', showPreview); return () => window.removeEventListener('forge:preview-open', showPreview); }, [hud, page]);
  const [settings, setSettings] = useState<Settings>(DEFAULT_SETTINGS); const [projects, setProjects] = useState<Project[]>([]); const [chats, setChats] = useState<Chat[]>([]); const [models, setModels] = useState<Model[]>([]);
  const [projectId, setProjectId] = useState<string | null>(null); const [chatId, setChatId] = useState<string | null>(null); const [chat, setChat] = useState<SavedChat | null>(null);
  const [drafts, setDraftsState] = useState<Record<string, string>>({}); const [attachments, setAttachmentsState] = useState<Record<string, string[]>>({}); const [lives, setLives] = useState<Record<string, LiveRun>>({});
  const [commandModes,setCommandModesState]=useState<Record<string,DraftCommandMode|null>>({});
  const [documents, setDocumentsState] = useState<Record<string, DocumentAttachment[]>>({});
  const [pendingAttachments, setPendingAttachmentsState] = useState<Record<string, PendingAttachment[]>>({});
  const [scheduledCount, setScheduledCount] = useState(0);
  const [taskSkills, setTaskSkillsState] = useState<Record<string,string[]>>({});
  const [taskSpaces, setTaskSpacesState] = useState<Record<string,string[]>>({});
  const [spaces, setSpaces] = useState<{id:string;name:string;project_ids?:string[]}[]>([]);
  const [pendingQuestionCount, setPendingQuestionCount] = useState(0);
  const [capabilities, setCapabilities] = useState<Record<string, unknown>>({}); const [connection, setConnection] = useState('Connecting'); const [notice, setNotice] = useState(''); const [busy, setBusy] = useState(false); const [search, setSearch] = useState('');
  const [recording, setRecording] = useState(false); const [transcribing, setTranscribing] = useState(false); const [dictationBusy, setDictationBusy] = useState(false); const [dictationSetupNeeded, setDictationSetupNeeded] = useState(false); const [captureBusy, setCaptureBusy] = useState(false); const [goalVersion, setGoalVersion] = useState(0); const [rename, setRename] = useState(false); const [renameValue, setRenameValue] = useState('');
  const [renameTarget,setRenameTarget]=useState<string|null>(null), [renameBusy,setRenameBusy]=useState(false), renameGeneration=useRef(0);
  const [messagesLimit, setMessagesLimit] = useState(80); const [readyPlan, setReadyPlan] = useState<SavedPlan | null>(null); const [planPreview, setPlanPreview] = useState(false);
  const [jumpToLatest,setJumpToLatest]=useState(false), [historyLoading,setHistoryLoading]=useState(false);const visibleDocument=useWorkspaceVisibility();
  const historyAnchor=useRef<{height:number;top:number}|null>(null);
  const [authNeeded, setAuthNeeded] = useState(false); const [authInput, setAuthInput] = useState('');
  const booted = useRef(false),bootstrapGeneration=useRef(0),bootstrapNavigation=useRef<number|null>(null);
  const [workspaceReady,setWorkspaceReady]=useState(false);
  const workspaceReadyRef=useRef(false);
  const liveRef = useRef(lives); const chatRef = useRef(chatId); const settingsQueue = useRef(Promise.resolve()); const settingsRevision = useRef(0); const polling = useRef(false); const chatScroll = useRef<HTMLDivElement>(null); const promptRef = useRef<HTMLTextAreaElement>(null); const fileInput = useRef<HTMLInputElement>(null); const shouldScroll = useRef(true); const dictationTarget = useRef(''); const dictationJob = useRef('');
  const recorder = useRef<MediaRecorder | null>(null); const recordingStream = useRef<MediaStream | null>(null); const browserAudio = useRef<Blob[]>([]); const recordingTimer = useRef<ReturnType<typeof setTimeout> | null>(null); const recordingCancelled = useRef(false);
  const dictationScope=useRef<import('./persistentDraft').DraftScope|undefined>(undefined),dictationDeliveries=useRef(new Set<string>()),dictationEpoch=useRef(0);
  const chatNavigationVersion = useRef(0);
  const newComposerFocus = useRef<number | null>(null);
  function returnToChat() { const previous = navigation.current; setPage('chat'); if (navigation.current !== previous) newComposerFocus.current = navigation.current; }
  useLayoutEffect(() => {
    const accepted = newComposerFocus.current;
    if (accepted === null) return;
    newComposerFocus.current = null;
    if (accepted === navigation.current && page === 'chat' && updateReadiness.canEdit()) promptRef.current?.focus();
  });
  const steerRequests = useRef(new Map<string, string>());
  const sendRequests = useRef(new Map<string, string>());
  const [failedSubmissions,setFailedSubmissions]=useState<Record<string,SubmissionScope>>({});
  const handoff=useUpdateReadiness(), workspaceRoot=useRef<HTMLDivElement>(null);
  useLayoutEffect(installReadinessFence,[]);
  useLayoutEffect(()=>{const lock=()=>{if(workspaceRoot.current)workspaceRoot.current.inert=updateReadiness.isLocked();};lock();return updateReadiness.subscribe(lock);},[]);
  const privateComposers=useRef({drafts,documents,taskSkills,taskSpaces,attachments,commandModes});
  const draftScopes=useRef(new Map<string,import('./persistentDraft').DraftScope>());
  const deletedChats=useRef(new Set<string>());
  function updateComposerMap<K extends keyof typeof privateComposers.current>(name:K, value:typeof privateComposers.current[K] | ((previous:typeof privateComposers.current[K])=>typeof privateComposers.current[K])) {
    updateReadiness.changed();
    const previous=privateComposers.current[name], next={...(typeof value==='function' ? value(previous) : value)};
    for(const id of deletedChats.current)delete next[id];
    privateComposers.current={...privateComposers.current,[name]:next};
    const state=privateComposers.current;
    for(const changedKey of Object.keys(next)) {
      if(next[changedKey]!==previous[changedKey])composerSnapshots.current.set(changedKey,JSON.stringify({text:state.drafts[changedKey]||'',document_ids:(state.documents[changedKey]||[]).map(item=>item.id),skills:state.taskSkills[changedKey]||[],space_ids:state.taskSpaces[changedKey]||[],...(state.commandModes[changedKey]?{command_mode:state.commandModes[changedKey]}:{}),images:state.attachments[changedKey]||[]}));
      if(next[changedKey]===previous[changedKey] || !workspaceReadyRef.current)continue;
      const owned=draftScopes.current.get(changedKey); if(!owned || name==='attachments')continue;
      persistedDraft.captureScope(owned,{text:state.drafts[changedKey] || '',document_ids:(state.documents[changedKey] || []).map(item=>item.id),skills:state.taskSkills[changedKey] || [],space_ids:state.taskSpaces[changedKey] || [],...(state.commandModes[changedKey]?{command_mode:state.commandModes[changedKey]}:{})});
    }
    if(name==='drafts')setDraftsState(next as typeof drafts);else if(name==='documents')setDocumentsState(next as typeof documents);else if(name==='taskSkills')setTaskSkillsState(next as typeof taskSkills);else if(name==='taskSpaces')setTaskSpacesState(next as typeof taskSpaces);else if(name==='commandModes')setCommandModesState(next as typeof commandModes);else setAttachmentsState(next as typeof attachments);
  }
  const setCommandModes=(value:typeof commandModes | ((previous:typeof commandModes)=>typeof commandModes))=>updateComposerMap('commandModes',value);
  const setDrafts=(value:typeof drafts | ((previous:typeof drafts)=>typeof drafts))=>updateComposerMap('drafts',value);
  const setDocuments=(value:typeof documents | ((previous:typeof documents)=>typeof documents))=>updateComposerMap('documents',value);
  const setTaskSkills=(value:typeof taskSkills | ((previous:typeof taskSkills)=>typeof taskSkills))=>updateComposerMap('taskSkills',value);
  const setTaskSpaces=(value:typeof taskSpaces | ((previous:typeof taskSpaces)=>typeof taskSpaces))=>updateComposerMap('taskSpaces',value);
  const setAttachments=(value:typeof attachments | ((previous:typeof attachments)=>typeof attachments))=>updateComposerMap('attachments',value);
  const setPendingAttachments=(value:typeof pendingAttachments | ((previous:typeof pendingAttachments)=>typeof pendingAttachments))=>setPendingAttachmentsState(previous=>{const next={...(typeof value==='function' ? value(previous) : value)};for(const id of deletedChats.current)delete next[id];return next;});
  const [slashIndex, setSlashIndex] = useState(0), [slashDismissed, setSlashDismissed] = useState(false);
  const queuedPreferences=useRef(0),preferencesFailed=useRef(false);
  const localInputs=useRef({attachments:0,dictation:0});
  liveRef.current = lives; chatRef.current = chatId;
  useEffect(()=>{const update=(event:Event)=>{const detail=(event as CustomEvent<{run_id:string;planner_assignment?:Run['planner_assignment']}>).detail;if(!detail?.run_id||!detail.planner_assignment)return;setLives(previous=>previous[detail.run_id]?{...previous,[detail.run_id]:{...previous[detail.run_id],run:{...previous[detail.run_id].run,planner_assignment:detail.planner_assignment}}}:previous);};window.addEventListener('forge:goal-guidance',update);return()=>window.removeEventListener('forge:goal-guidance',update);},[]);
  const draftKey = chatId || `new:${projectId || 'global'}`;
  if(!deletedChats.current.has(draftKey))draftScopes.current.set(draftKey,chatId ? {scope_kind:'chat',chat_id:chatId,project_id:projectId} : {scope_kind:'new_chat',project_id:projectId});
  const draft = drafts[draftKey] || ''; const commandMode=commandModes[draftKey] || null; const images = attachments[draftKey] || [];
  const documentAttachments = documents[draftKey] || [];
  const processingAttachments = (pendingAttachments[draftKey] || []).some(item => item.state === 'processing');
  const composerContent = {text: draft, document_ids: documentAttachments.map(item => item.id), skills: taskSkills[draftKey] || [], space_ids: taskSpaces[draftKey] || [],...(commandMode?{command_mode:commandMode}:{})};
  const composerFingerprint = JSON.stringify({...composerContent, images});
  const composerRef = useRef({key: draftKey, fingerprint: composerFingerprint}); composerRef.current = {key: draftKey, fingerprint: composerFingerprint};
  const composerSnapshots = useRef(new Map<string,string>()); composerSnapshots.current.set(draftKey, composerFingerprint);
  const persistedDraft = usePersistentDraft(chatId ? {scope_kind: 'chat', chat_id: chatId, project_id: projectId} : {scope_kind: 'new_chat', project_id: projectId}, composerContent, content => {
    if(!Object.keys(content).length) setAttachments(previous=>({...previous,[draftKey]:[]}));
    setCommandModes(previous=>({...previous,[draftKey]:isComposerMode(content.command_mode)?content.command_mode:null}));
    setDrafts(previous => ({...previous, [draftKey]: content.text || ''}));
    setDocuments(previous => ({...previous, [draftKey]: (content.document_ids || []).map(id => ({id, name: documentAttachments.find(item => item.id === id)?.name || 'Saved document'}))}));
    setTaskSkills(previous => ({...previous, [draftKey]: content.skills || []}));
    setTaskSpaces(previous => ({...previous, [draftKey]: content.space_ids || []}));
  }, workspaceReady);
  useEffect(() => {setSlashIndex(0); setSlashDismissed(false);}, [draft]);
  useEffect(() => { let alive=true; void api<{spaces:{id:string;name:string;project_ids?:string[]}[]}>('spaces').then(result=>{if(alive)setSpaces(result.spaces || []);}).catch(()=>{}); return()=>{alive=false;}; },[projectId]);
  const project = projects.find(p => p.id === projectId);
  const recentRuns = Object.values(lives).sort((a, b) => (Date.parse(b.run.created_at || '') || 0) - (Date.parse(a.run.created_at || '') || 0));
  const lastRun = recentRuns.find(l => l.run.chat_id === chatId);
  const live = recentRuns.find(l => l.run.chat_id === chatId && !l.finished) || (lastRun && ['paused', 'interrupted', 'failed'].includes(lastRun.run.status || '') ? lastRun : undefined);
  const visibleLive = live ? reconcileRunMessages(live, chat) : undefined;
  useLayoutEffect(() => { thinkingFocus.current.revision++; }, [page, chatId, projectId, hud]);
  const liveThinking = live ? thinkingReadOwners(live, chat, thinkingReads.current, bootstrapGeneration.current, true, thinkingFocus.current) : undefined;
  const savedThinking = new Map(liveThinking?.saved);
  for (const item of recentRuns) if (item !== live && item.run.chat_id === chatId) {
    const owners = thinkingReadOwners(item, chat, thinkingReads.current, bootstrapGeneration.current, false, thinkingFocus.current);
    for (const [id, intent] of owners.saved) savedThinking.set(id, intent);
  }
  const visiblePartOffset = live && visibleLive ? live.parts.length - visibleLive.parts.length : 0;
  const speed = live?.speed ?? lastRun?.speed;
  const speedEstimated = live?.speedEstimated ?? lastRun?.speedEstimated;
  const running = Boolean(live && !live.finished);
  const speedLabel = speed !== undefined ? `${speed.toFixed(1)}${speedEstimated ? ' (estimated)' : ''}` : running ? '— (estimated)' : '—';
  const activeRuns = Object.values(lives).filter(l => !l.finished);
  const setDraft = (text: string) => {if(!updateReadiness.canEdit())return;const selected=consumeWorkflowPrefix(text);if(selected)setCommandModes(previous=>({...previous,[draftKey]:selected.mode}));setDrafts(prev => ({ ...prev, [draftKey]: selected?selected.text:text }));};
  const selectSlash=(text:string)=>{setDraft(text);promptRef.current?.focus();};
  useReadinessParticipant({kind:'workspace',inspect:()=>{
    const reasons:ReadinessReason[]=[];
    if(!workspaceReady || authNeeded)reasons.push('workspace_loading');
    if(Object.values(privateComposers.current.attachments).some(items=>items.length))reasons.push('unsent_images');
    if(localInputs.current.attachments || captureBusy || Object.values(pendingAttachments).some(items=>items.some(item=>item.state==='processing')))reasons.push('attachment_pending');
    if(localInputs.current.dictation || recording || transcribing || dictationBusy || recorder.current || recordingStream.current || browserAudio.current.length || dictationJob.current)reasons.push('dictation_pending');
    if(busy)reasons.push('submission_pending');
    if(queuedPreferences.current || preferencesFailed.current)reasons.push('preferences_pending');
    if(Object.keys(failedSubmissions).length || sendRequests.current.size || steerRequests.current.size)reasons.push('submission_unknown');
    if(activeRuns.length)reasons.push('active_work');
    if(Object.values(lives).some(item=>item.approval))reasons.push('approval_pending');
    if(pendingQuestionCount)reasons.push('question_pending');
    if(Object.values(lives).some(item=>item.tools.some(tool=>tool.type==='outcome_unknown' || tool.state==='Outcome unknown')))reasons.push('recovery_pending');
    return {reasons,state:preferencesFailed.current?'save_failed':queuedPreferences.current?'waiting':!workspaceReady || authNeeded?'loading':'ready',pending:queuedPreferences.current};
  },flushAndConfirm:async()=>{await settingsQueue.current;return {ok:true,receipts:[]};}});

  const flash = useCallback((message: string) => { setNotice(message); setDictationSetupNeeded(false); }, []);
  const workspaceLayout=useWorkspaceLayout(workspaceReady,flash), uiAppearance=appearance(settings);
  const sidebar=compactViewport?smallSidebar:workspaceLayout.layout.sidebar_open;
  const setSidebar=(open:boolean)=>{if(compactViewport)setSmallSidebar(open);else workspaceLayout.update({sidebar_open:open});};
  const layoutReady=useRef(false);
  useEffect(()=>{if(workspaceLayout.ready && !layoutReady.current){layoutReady.current=true;if(!panelTouched.current){setRightPanelState(workspaceLayout.layout.right_panel_open);setPanelTabState(workspaceLayout.layout.right_panel_tab);}else workspaceLayout.update({right_panel_open:rightPanel,right_panel_tab:panelTab});}},[workspaceLayout.ready]);
  useEffect(()=>{if(layoutReady.current && panelTouched.current)workspaceLayout.update({right_panel_open:rightPanel,right_panel_tab:panelTab});},[rightPanel,panelTab]);
  useEffect(()=>{document.documentElement.dataset.accent=uiAppearance.accent;document.documentElement.dataset.motion=uiAppearance.motion;},[uiAppearance.accent,uiAppearance.motion]);
  useEffect(()=>{const changed=(event:Event)=>{const saved=(event as CustomEvent<Project>).detail;setProjects(previous=>previous.map(item=>item.id===saved.id?saved:item));};window.addEventListener('forge:project-appearance',changed);return()=>window.removeEventListener('forge:project-appearance',changed);},[]);
  const activities=transcriptActivity(chat,recentRuns), activityOwners=activityRunIds(activities);
  const activitiesFor=(id:Message['id'])=>activities.filter(group=>Number(group.request_message_id)===Number(id));
  const unanchoredActivities=activities.filter(group=>!chat?.messages.some(message=>Number(message.id)===Number(group.request_message_id)));
  async function refreshModels(accepted=()=>true) {
    try { const result = await api<{ models: Model[] }>('models'); if(!accepted())return;setModels(result.models || []); setConnection('Local engine'); }
    catch (e) { if(accepted()){setConnection('Engine offline'); flash(errorText(e));} }
  }
  async function refreshWorkspace(accepted=()=>true) {
    const results = await Promise.allSettled([api<{ projects: Project[] }>('projects'), api<{ chats: Chat[] }>('chats', { archived: 'all' }), api<{schedules: {enabled?: boolean}[]}>('schedules')]);
    if(!accepted())return;
    if (results[0].status === 'fulfilled') setProjects(results[0].value.projects || []);
    if (results[1].status === 'fulfilled') setChats(results[1].value.chats || []);
    if (results[2].status === 'fulfilled') setScheduledCount((results[2].value.schedules || []).filter(item => item.enabled !== false).length);
  }
  async function loadChat(id: string, limit = 80, older = false, accepted=()=>true) {
    if(!accepted() || !updateReadiness.canEdit() || deletedChats.current.has(id))return;
    const event = new CustomEvent('forge:navigate', {cancelable: true, detail: {page: 'chat', chat_id: id}}); if (!window.dispatchEvent(event)) return;
    const navigationVersion=++navigation.current;
    const version = ++chatNavigationVersion.current;
    if(older && chatScroll.current){historyAnchor.current={height:chatScroll.current.scrollHeight,top:chatScroll.current.scrollTop};setHistoryLoading(true);}else{historyAnchor.current=null;setJumpToLatest(false);}
    try {
      const next = await api<SavedChat>('get_chat', { id, limit }); if (!accepted() || deletedChats.current.has(id) || version !== chatNavigationVersion.current || navigationVersion!==navigation.current) return; if(older && chatScroll.current)historyAnchor.current={height:chatScroll.current.scrollHeight,top:chatScroll.current.scrollTop};setChat(next); setChatId(next.id); setProjectId(next.project_id); setMessagesLimit(limit); setPageState('chat');
      if (next.model) setSettings(prev => ({ ...prev, model: next.model }));
      shouldScroll.current = !older;
    } catch (e) { if(accepted() && version===chatNavigationVersion.current){historyAnchor.current=null;flash(errorText(e));} }
    finally{if(accepted() && version===chatNavigationVersion.current)setHistoryLoading(false);}
  }
  async function bootstrap() {
    if (booted.current) return; booted.current = true;
    thinkingReads.current.clear();
    const generation=++bootstrapGeneration.current,transport=isNative()?window.pywebview!.api:null;
    const accepted=()=>generation===bootstrapGeneration.current && transport===(isNative()?window.pywebview!.api:null);
    // Ref-only activation can be cached in a React bailout without running its
    // effects. Saved-work owners activate through committed readiness state.
    workspaceReadyRef.current=false;setWorkspaceReady(false);
    try {
      const result = await api<Record<string, unknown>>('bootstrap');
      if(!accepted())return;
      const saved = result.settings as Partial<Settings> || {};
      setSettings(prev => ({ ...prev, ...saved })); setProjects(listFrom<Project>(result, 'projects')); const savedChats = listFrom<Chat>(result, 'chats'); setChats(savedChats); setCapabilities(result.capabilities as Record<string, unknown> || {});
      const restored: Record<string, LiveRun> = {};
      for (const run of listFrom<Run>(result, 'runs')) if (ACTIVE.has(run.status || '') || ['paused', 'interrupted', 'failed', 'completed'].includes(run.status || '')) restored[run.id] = { run, text: '', thinking: '', parts: [], tools: [], cursor: 0, status: run.status || 'running', finished: !ACTIVE.has(run.status || '') };
      void api<{ first_run: boolean }>('setup_status').then(r => { if (accepted() && r.first_run) setShowSetup(true); }).catch(() => {});
      setLives(restored); setConnection('Coordinator ready'); setAuthNeeded(false);
      workspaceReadyRef.current=true;setWorkspaceReady(true);
      // Keep work typed before startup in its original scope, including a new
      // conversation hidden by automatic history restoration.
      const state=privateComposers.current;
      for(const [key,scope] of draftScopes.current){
        const content={text:state.drafts[key] || '',document_ids:(state.documents[key] || []).map(item=>item.id),skills:state.taskSkills[key] || [],space_ids:state.taskSpaces[key] || [],...(state.commandModes[key]?{command_mode:state.commandModes[key]}:{})};
        if(content.text || content.document_ids.length || content.skills.length || content.space_ids.length || content.command_mode)persistedDraft.captureScope(scope,content);
      }
      // Initial history restoration is subordinate to navigation already chosen
      // by the user, including a click before bootstrap begins.
      if (savedChats[0] && (navigation.current===0 || bootstrapNavigation.current===navigation.current)) {
        const previousNavigation=navigation.current,restoring=loadChat(savedChats[0].id,80,false,accepted);
        if(navigation.current===previousNavigation+1)bootstrapNavigation.current=navigation.current;
        await restoring;
      }
      if(!accepted())return;
      void refreshWorkspace(accepted);
      void refreshModels(accepted);
    } catch (e) { if(!accepted())return;booted.current = false;workspaceReadyRef.current=false;setWorkspaceReady(false);setConnection('Disconnected'); flash(errorText(e)); if (/401|authenticate|unauthoriz|token|pair/i.test(errorText(e))) setAuthNeeded(true); }
  }
  useEffect(() => {
    const ready = () => { booted.current = false; void bootstrap(); };
    window.addEventListener('pywebviewready', ready);
    const timer = setTimeout(() => void bootstrap(), 300);
    return () => { bootstrapGeneration.current++;clearTimeout(timer); window.removeEventListener('pywebviewready', ready); };
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
  useLayoutEffect(() => {
    const el=chatScroll.current;if(!el)return;
    if(historyAnchor.current){const anchor=historyAnchor.current;el.scrollTop=anchor.top+el.scrollHeight-anchor.height;historyAnchor.current=null;return;}
    if(shouldScroll.current){el.scrollTop=el.scrollHeight;setJumpToLatest(false);}else setJumpToLatest(el.scrollHeight-el.scrollTop-el.clientHeight>=120);
  }, [chat?.messages.length, live?.text, live?.thinking, live?.parts.length]);
  useEffect(() => {
    const tick = async () => {
      if (polling.current) return; const current = Object.values(liveRef.current).filter(l => !l.finished); if (!current.length) return;
      polling.current = true;
      try {
        const batches = await Promise.allSettled(current.map(async item => ({ id: item.run.id, result: await api<{ events: RunEvent[]; next_cursor?: number; status?: string; finished?: boolean; chat_id?: string }>('poll', { id: item.run.id, after: item.cursor }) })));
        const failed = batches.find(batch => batch.status === 'rejected');
        if (failed?.status === 'rejected') setConnection('Reconnecting'); else setConnection('Coordinator ready');
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
  async function changeSettings(delta: Partial<Settings>, requireAcknowledgement=false) {
    if(!updateReadiness.canEdit()){if(requireAcknowledgement)throw new Error('Finish restart preparation before changing context.');return;} updateReadiness.changed();
    queuedPreferences.current++;
    const revision = ++settingsRevision.current;
    const requestedBridge=window.pywebview?.api,requestedCall=requestedBridge?.call;
    const sameTransport=()=>window.pywebview?.api===requestedBridge && window.pywebview?.api?.call===requestedCall;
    setSettings(prev => ({ ...prev, ...delta }));
    settingsQueue.current = settingsQueue.current.catch(() => {}).then(async () => {
      try { if(requireAcknowledgement && !sameTransport())throw new Error('Context was not acknowledged. Retry the context save.');if ('startup' in delta) await api('startup', { enabled: delta.startup }); const saved = await api<Settings>('settings', delta);if(requireAcknowledgement && (!sameTransport() || !saved || 'context' in delta && saved.context!==delta.context || 'adaptive_context_locked' in delta && saved.adaptive_context_locked!==delta.adaptive_context_locked))throw new Error('Context was not acknowledged. Retry the context save.');preferencesFailed.current=false; if (revision === settingsRevision.current) setSettings(prev => ({ ...prev, ...saved })); }
      catch (e) { preferencesFailed.current=true;flash(`Could not save preferences: ${errorText(e)}`);if(requireAcknowledgement)throw e; }
      finally {queuedPreferences.current--;}
    });
    await settingsQueue.current;
  }
  async function switchMode(mode: boolean, expand = false) {
    if(!updateReadiness.canEdit())return;
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
      if(!updateReadiness.canEdit()){if((e.ctrlKey && ['n','h'].includes(e.key.toLowerCase())) || e.shiftKey && e.key==='Escape' || e.target instanceof Element && e.target.closest('.app'))e.preventDefault();return;}
      if (e.ctrlKey && e.shiftKey && e.key.toLowerCase() === 'h') { e.preventDefault(); void switchMode(!hud); }
      if (e.ctrlKey && e.key.toLowerCase() === 'n') { e.preventDefault(); newChat(); }
      if (e.key === 'Escape') { setModelOpen(false); if (running && e.shiftKey) void controlRun('cancel'); }
    }; window.addEventListener('keydown', keys); return () => window.removeEventListener('keydown', keys);
  }, [hud, running, live]);
  function newChat() { if(!updateReadiness.canEdit())return; const event = new CustomEvent('forge:navigate', {cancelable:true,detail:{page:'chat',action:'new'}}); if (!window.dispatchEvent(event)) return; chatNavigationVersion.current++; navigation.current++; newComposerFocus.current = navigation.current; chatRef.current = null; setChatId(null); setChat(null); setMessagesLimit(80); setReadyPlan(null); setPageState('chat'); if (promptRef.current) { promptRef.current.focus(); newComposerFocus.current = null; } }
  function chooseProject(id: string | null) { if(!updateReadiness.canEdit())return; const event = new CustomEvent('forge:navigate', {cancelable:true,detail:{page:'chat',action:'project',project_id:id}}); if (!window.dispatchEvent(event)) return; chatNavigationVersion.current++; navigation.current++; chatRef.current = null; setProjectId(id); setChatId(null); setChat(null); setPageState('chat'); }
  useEffect(()=>{const approved=(event:Event)=>{const detail=(event as CustomEvent).detail;if(detail.action==='new')newChat();else if(detail.action==='project')chooseProject(detail.project_id);else if(detail.chat_id)void loadChat(detail.chat_id);else if(detail.page)setPage(detail.page);};window.addEventListener('forge:navigate-approved',approved);return()=>window.removeEventListener('forge:navigate-approved',approved);},[page,chatId]);
  async function chatChanged(id: string, change: ChatChange) {
    if(change==='delete'){
      // ChatRow calls this only after the coordinator acknowledges deletion.
      // Revoke the scope before refreshes or any late input callback can arrive.
      deletedChats.current.add(id);persistedDraft.forgetChat(id);draftScopes.current.delete(id);composerSnapshots.current.delete(id);
      for (const [key, record] of thinkingReads.current) if (record.chatId === id) thinkingReads.current.delete(key);
      setLives(prev=>Object.fromEntries(Object.entries(prev).filter(([,item])=>item.run.chat_id!==id)));
      setCommandModes(prev=>({...prev}));setDrafts(prev=>({...prev}));setAttachments(prev=>({...prev}));setDocuments(prev=>({...prev}));setTaskSkills(prev=>({...prev}));setTaskSpaces(prev=>({...prev}));setPendingAttachments(prev=>({...prev}));
      if(dictationTarget.current===id){
        recordingCancelled.current=true;dictationEpoch.current++;
        if(recordingTimer.current)clearTimeout(recordingTimer.current);
        if(recorder.current?.state==='recording')recorder.current.stop();recordingStream.current?.getTracks().forEach(track=>track.stop());browserAudio.current=[];
        const job=dictationJob.current;dictationJob.current='';setRecording(false);setTranscribing(false);
        if(job && job!=='pending-upload')void api('dictation_cancel',{id:job}).catch(()=>{});
      }
      if(id===chatRef.current)newChat();
    }
    await refreshWorkspace();
    if (id === chatRef.current) { if (change === 'delete' || change === 'archive') newChat(); else await loadChat(id); }
    flash(change === 'delete' ? 'Conversation deleted.' : change === 'archive' ? 'Conversation archived.' : change === 'restore' ? 'Conversation restored.' : change === 'move' ? 'Conversation moved.' : 'Conversation renamed.');
  }
  async function projectRemoved(id: string) { await refreshWorkspace(); if (projectId === id) { setProjectId(null); if (chatId) await loadChat(chatId); } flash('Project registration removed. Its folder was kept.'); }
  function openChatRename(){if(page!=='chat'||!chatId)return;renameGeneration.current++;setRenameTarget(chatId);setRenameValue(chat?.title || '');setRenameBusy(false);setRename(true);}
  function closeChatRename(){renameGeneration.current++;setRename(false);setRenameTarget(null);setRenameBusy(false);}
  async function saveChatRename(){const target=renameTarget,title=renameValue.trim(),generation=renameGeneration.current;if(!target||!title||renameBusy)return;setRenameBusy(true);try{await api('rename_chat',{id:target,title});if(generation===renameGeneration.current){setChat(previous=>previous?.id===target?{...previous,title}:previous);closeChatRename();}await refreshWorkspace();}catch(failure){if(generation===renameGeneration.current)flash(errorText(failure));}finally{if(generation===renameGeneration.current)setRenameBusy(false);}}
  async function registerProject() { try { if (isNative()) { const result = await native<Project & { cancelled?: boolean }>('choose_project'); if (!result.cancelled) { await refreshWorkspace(); chooseProject(result.id); } } else setPage('projects'); } catch (e) { flash(errorText(e)); } }
  function trackRun(run: Run, navigate = true) {
    if(updateReadiness.isLocked())updateReadiness.changed();
    if (navigate) {chatNavigationVersion.current++; navigation.current++;}
    setLives(prev => {const existing=prev[run.id];return {...prev,[run.id]:existing ? {...existing,run:{...existing.run,...run},finished:!ACTIVE.has(run.status || 'running')} : { run: { ...run, created_at: run.created_at || new Date().toISOString(), model: run.model || run.settings?.model || settings.model }, text: '', thinking: '', parts: [], tools: [], cursor: 0, status: 'Loading model / waiting for engine…', finished: !ACTIVE.has(run.status || 'running') }};});
    if (navigate && run.chat_id) { setChatId(run.chat_id); if (run.chat_id !== chatId) setChat({ id: run.chat_id, project_id: run.project_id || projectId, model: run.model || settings.model, title: draft.slice(0, 60), messages: [] }); }
  }
  async function send(override?: string) {
    if(!workspaceReady || !updateReadiness.canEdit())return;
    const text = (override || draft).trim(); if (!text || busy || processingAttachments) return;
    const bareWorkflow=/^\/(goal|plan|do|builder|todo)$/i.exec(text);if(bareWorkflow){selectSlash(`/${bareWorkflow[1]} `);return;}
    const user: Message = { role: 'user', content: text, images: images.length ? images : undefined };
    const submitted = {key:draftKey, fingerprint:composerFingerprint, draftScope:persistedDraft.key, draftContent:JSON.stringify(composerContent), navigation:navigation.current, projectId, chatId, commandMode, settings:{...settings}, images:[...images], documents:documentAttachments.map(item=>item.id), spaces:[...(taskSpaces[draftKey] || [])], skills:[...(taskSkills[draftKey] || [])]};
    const action=text.startsWith('/') || submitted.commandMode?'command':'start_chat';
    const mode=requestMode(text,submitted.commandMode);
    const requestKey = JSON.stringify({...submitted, navigation:undefined, fingerprint:undefined, key:undefined, text});
    if (running && !sendRequests.current.has(requestKey) && !/^\/(?:pause|status|help|project|model|agents|worktree|skill|mcp|schedule|new)(?:\s|$)/i.test(text)) { flash('Pause this run before starting another workflow. Use Steer to guide its current task.'); return; }
    setBusy(true); setNotice('');
    const submissionId = sendRequests.current.get(requestKey) || crypto.randomUUID(); sendRequests.current.set(requestKey, submissionId);
    try {
      await settingsQueue.current;
      await persistedDraft.flush();
      const payload = { ...submitted.settings, model: submitted.settings.model, text, project_id: submitted.projectId, chat_id: submitted.chatId, images: submitted.images, document_ids: submitted.documents, space_ids: submitted.spaces, ...(submitted.skills.length ? {skills: [...(Array.isArray(submitted.settings.skills) ? submitted.settings.skills as string[] : []), ...submitted.skills]} : {}), permission_profile: submitted.settings.permission_profile, client_submission_id:submissionId,...(submitted.commandMode?{command_mode:submitted.commandMode}:{}) };
      const result = await api<Run & { navigate?: string; message?: string; goal?: unknown; mode?: string; chat?: Chat }>(action, payload);
      const stillHere = navigation.current === submitted.navigation;
      sendRequests.current.delete(requestKey);
      setFailedSubmissions(previous=>{const next={...previous};delete next[submitted.key];return next;});
      if (result.navigate && stillHere) {
        const target = result.navigate.toLowerCase();
        if (target === 'models' || target === 'mcp' || target === 'skills' || target === 'worktrees') { setSettingsTab(target === 'models' ? 'models' : target === 'mcp' ? 'mcp' : 'integrations'); setPage(target === 'worktrees' ? 'projects' : 'settings'); }
        else if (target === 'new') newChat(); else if (target === 'chat' && result.chat) await loadChat(result.chat.id); else if (target === 'goals') { setRightPanel(true); setPanelTab('goals'); setPage('chat'); } else if (navItems.some(n => n.page === target) || target === 'settings') setPage(target as Page);
      }
      if (result.id) {
        user.id=result.request_message_id;
        trackRun({ ...result, mode: mode==='plan'?'plan':result.mode, request: text.startsWith('/plan ') ? text.slice(6).trim() : text }, stillHere);
        if (stillHere) {setChat(prev => prev && prev.id === (result.chat_id || submitted.chatId) ? result.request_message_id && prev.messages.some(message=>String(message.id)===String(result.request_message_id)) ? prev : { ...prev, messages: [...prev.messages, user] } : { id: result.chat_id || submitted.chatId || '', project_id: submitted.projectId, title: text.slice(0, 60), model: submitted.settings.model, messages: [user] }); setReadyPlan(null); shouldScroll.current = true;}
        if (stillHere && mode==='todo') { setRightPanel(true); setPanelTab('goals'); setPage('chat'); }
      }
      if (result.message) flash(result.message);
      if (composerSnapshots.current.get(submitted.key) === submitted.fingerprint) {
        await persistedDraft.clearSubmitted(submitted.draftScope, submitted.draftContent);
        if (composerSnapshots.current.get(submitted.key) === submitted.fingerprint) {if(mode)setCommandModes(prev=>({...prev,[submitted.key]:null}));setDrafts(prev => ({ ...prev, [submitted.key]: '' })); setAttachments(prev => ({ ...prev, [submitted.key]: [] })); setDocuments(prev=>({...prev,[submitted.key]:[]})); setTaskSkills(prev=>({...prev,[submitted.key]:[]})); setTaskSpaces(prev=>({...prev,[submitted.key]:[]}));}
      }
      setGoalVersion(v => v + 1); void refreshWorkspace();
    } catch (e) { setFailedSubmissions(previous=>({...previous,[submitted.key]:{action,client_submission_id:submissionId,project_id:submitted.projectId,chat_id:submitted.chatId}}));flash(errorText(e)); }
    finally { setBusy(false); if (navigation.current === submitted.navigation) promptRef.current?.focus(); }
  }
  async function buildPlan() {
    if(!updateReadiness.canEdit())return;
    if (!readyPlan || busy || running) return; setBusy(true); setNotice('');
    const submitted={run_id:readyPlan.run_id,navigation:navigation.current,key:draftKey};const requestKey=`plan:${submitted.run_id}`;const clientId=sendRequests.current.get(requestKey)||crypto.randomUUID();sendRequests.current.set(requestKey,clientId);
    try { await settingsQueue.current; const run = await api<Run>('plan_build', { run_id: submitted.run_id,client_submission_id:clientId });sendRequests.current.delete(requestKey);const stillHere=navigation.current===submitted.navigation;trackRun(run,stillHere);if(stillHere){setReadyPlan(null); setPlanPreview(false); setRightPanel(true); setPanelTab('goals');}setGoalVersion(value => value + 1); await refreshWorkspace(); }
    catch (error) {setFailedSubmissions(previous=>({...previous,[submitted.key]:{action:'plan_build',client_submission_id:clientId,run_id:submitted.run_id}}));flash(errorText(error));} finally { setBusy(false); }
  }
  async function steer() {
    if(!updateReadiness.canEdit())return;
    const text = draft.trim(); const runId = live?.run.id;
    if (!text || !runId || !running || busy) return;
    // Slash actions remain commands; they must never become ambiguous model instructions.
    if (text.startsWith('/') || commandMode) { await send(); return; }
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
    if(!updateReadiness.canEdit())return;
    localInputs.current.attachments++;updateReadiness.changed();
    const key = draftKey; setCaptureBusy(true); try { const result = isNative() ? await native<{ image: string }>('screenshot') : await api<{ image: string }>('screenshot'); setAttachments(prev => ({ ...prev, [key]: [...(prev[key] || []), result.image] })); } catch (e) { flash(errorText(e)); } finally { localInputs.current.attachments--;updateReadiness.changed();setCaptureBusy(false); }
  }
  async function dictate() {
    if(!updateReadiness.canEdit())return;
    if (dictationBusy || localInputs.current.dictation) return;localInputs.current.dictation++;updateReadiness.changed(); setDictationBusy(true); setDictationSetupNeeded(false);
    try {
      if (transcribing) { recordingCancelled.current = true;dictationEpoch.current++; await api('dictation_cancel',dictationJob.current && dictationJob.current!=='pending-upload' ? {id:dictationJob.current} : {}); setTranscribing(false); dictationJob.current = ''; flash('Dictation cancelled.'); return; }
      if (!isNative()) {
        if (recording && recorder.current) { dictationJob.current = 'pending-upload'; recorder.current.stop(); setRecording(false); setTranscribing(true); return; }
        if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === 'undefined') throw new Error('Microphone recording requires a supported browser on localhost or HTTPS.');
        dictationTarget.current = draftKey;dictationScope.current=draftScopes.current.get(draftKey); dictationJob.current = ''; recordingCancelled.current = false;
        const generation=++dictationEpoch.current,ownedScope=dictationScope.current;
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });if(recordingCancelled.current || generation!==dictationEpoch.current){stream.getTracks().forEach(track=>track.stop());return;} recordingStream.current = stream; browserAudio.current = [];
        const capture = new MediaRecorder(stream); recorder.current = capture;
        capture.ondataavailable = event => { if (event.data.size) browserAudio.current.push(event.data); };
        capture.onstop = async () => {
          stream.getTracks().forEach(track => track.stop()); recordingStream.current = null; recorder.current = null;
          if (recordingTimer.current) clearTimeout(recordingTimer.current);
          const chunks = browserAudio.current; browserAudio.current = [];
          if (recordingCancelled.current || generation!==dictationEpoch.current) return;
          try { const audio = new Blob(chunks, { type: capture.mimeType }); if (audio.size > 6_000_000) throw new Error('Dictation audio exceeds 6 MB. Try a shorter recording.');
            const encoded = await new Promise<string>((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(String(reader.result).split(',')[1]); reader.onerror = reject; reader.readAsDataURL(audio); });
            if (recordingCancelled.current || generation!==dictationEpoch.current) return;
            const result = await api<{ id: string }>('dictation_upload', { audio: encoded,draft_scope:ownedScope });
            if (recordingCancelled.current || generation!==dictationEpoch.current) { await api('dictation_cancel',{id:result.id}); return; }
            dictationJob.current = result.id;
          } catch (e) { setTranscribing(false); flash(errorText(e)); }
          finally { chunks.length = 0; }
        };
        capture.start(1000); setRecording(true);
        recordingTimer.current = setTimeout(() => { if (capture.state === 'recording') { dictationJob.current = 'pending-upload'; capture.stop(); setRecording(false); setTranscribing(true); } }, 120000);
        return;
      }
      if (!recording) { dictationEpoch.current++;dictationTarget.current = draftKey;dictationScope.current=draftScopes.current.get(draftKey); dictationJob.current = ''; }
      const generation=dictationEpoch.current,ownedScope=dictationScope.current,target=dictationTarget.current;
      const result = await api<{ state: string; id?: string; text?: string; delivery_id?:string;message?: string; setup_required?: boolean }>(recording ? 'dictation_stop' : 'dictation_start',recording ? {} : {draft_scope:dictationScope.current});
      if(generation!==dictationEpoch.current){if(deletedChats.current.has(target) && result.id)void api('dictation_cancel',{id:result.id}).catch(()=>{});return;}
      if (!['recording', 'transcribing', 'done', 'cancelled'].includes(result.state)) { setDictationSetupNeeded(Boolean(result.setup_required)); throw new Error(result.message || 'Dictation could not start. Check Settings → Dictation.'); }
      setNotice('');
      setRecording(result.state === 'recording'); setTranscribing(result.state === 'transcribing'); if (result.id) dictationJob.current = result.id;
      if (result.state === 'done') {acceptDictation(result,target,ownedScope);dictationJob.current='';const text=result.text?.trim();flash(text ? 'Transcript added to your draft.' : 'No speech detected. Try again and check your microphone.'); }
    } catch (e) { setRecording(false); setTranscribing(false); dictationJob.current = ''; flash(errorText(e)); setDictationSetupNeeded(true); }
    finally { localInputs.current.dictation--;updateReadiness.changed();setDictationBusy(false); }
  }
  function acceptDictation(result:{text?:string;delivery_id?:string},target:string,owned:import('./persistentDraft').DraftScope|undefined) {
    if(deletedChats.current.has(target))return;
    const id=result.delivery_id;
    if(id && dictationDeliveries.current.has(id))return;
    const text=result.text?.trim();if(text)setDrafts(prev=>({...prev,[target]:`${prev[target] || ''}${prev[target] ? ' ' : ''}${text}`}));
    if(id && owned && /^[a-f0-9]{64}$/.test(id)){dictationDeliveries.current.add(id);persistedDraft.trackDelivery(owned,id);}
  }
  useEffect(() => {
    if ((!recording && !transcribing) || (recording && !isNative())) return;
    const generation=dictationEpoch.current,ownedScope=dictationScope.current,target=dictationTarget.current;
    let fetching = false;
    const timer = setInterval(async () => {
      if (fetching) return; fetching = true;
      try { const result = await api<{ state: string; id?: string; text?: string;delivery_id?:string; limit_reached?: boolean; message?: string }>('dictation_status');
        if(generation!==dictationEpoch.current)return;
        if (result.state === 'error') throw new Error(result.message || 'Dictation failed. Check your microphone and installed model.');
        if (result.state === 'done' && (!dictationJob.current || result.id === dictationJob.current)) {
          acceptDictation(result,target,ownedScope);
          setTranscribing(false); setRecording(false); dictationJob.current = ''; flash(result.text?.trim() ? 'Transcript added to your draft.' : 'No speech detected. Try again and check your microphone.');
        } else if (result.state === 'cancelled') { setTranscribing(false); setRecording(false); }
        else if (result.limit_reached && result.state === 'recording') { const next = await api<{ id: string }>('dictation_stop'); dictationJob.current = next.id; setRecording(false); setTranscribing(true); }
      } catch (e) { setRecording(false); setTranscribing(false); flash(errorText(e)); setDictationSetupNeeded(true); }
      finally { fetching = false; }
    }, 600); return () => clearInterval(timer);
  }, [recording, transcribing]);
  useEffect(() => () => { recordingCancelled.current = true;dictationEpoch.current++; if (recordingTimer.current) clearTimeout(recordingTimer.current); if (recorder.current?.state === 'recording') recorder.current.stop(); recordingStream.current?.getTracks().forEach(track => track.stop()); browserAudio.current = []; }, []);
  async function attachFiles(files: FileList | null) {
    if(!updateReadiness.canEdit())return;
    if (!files) return;
    const key=draftKey, targetProject=projectId;
    const selected=Array.from(files).slice(0,4).map(file=>({file,id:crypto.randomUUID()}));
    localInputs.current.attachments+=selected.length;updateReadiness.changed();
    setPendingAttachments(previous=>({...previous,[key]:[...(previous[key] || []),...selected.map(item=>({id:item.id,name:item.file.name,state:'processing' as const}))]}));
    for (const {file,id} of selected) {
      try {
        const image=file.type.startsWith('image/');
        if (file.size > (image ? 5_000_000 : 32*1024*1024)) throw new Error(image ? 'Choose an image below 5 MB.' : 'Choose a document below 32 MiB.');
        const content=await new Promise<string>((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(String(reader.result).split(',')[1]);reader.onerror=()=>reject(new Error('The file could not be read.'));reader.readAsDataURL(file);});
        if(deletedChats.current.has(key))continue;
        if (image) setAttachments(previous=>({...previous,[key]:[...(previous[key] || []),content].slice(0,4)}));
        else {const result=await api<DocumentAttachment>('document_upload',{name:file.name,content_base64:content,project_id:targetProject || undefined}); setDocuments(previous=>({...previous,[key]:[...(previous[key] || []),{id:result.id,name:file.name}].slice(0,8)}));}
        setPendingAttachments(previous=>({...previous,[key]:(previous[key] || []).filter(item=>item.id!==id)}));
      } catch(error) {setPendingAttachments(previous=>({...previous,[key]:(previous[key] || []).map(item=>item.id===id?{...item,state:'error',error:errorText(error)}:item)}));}
      finally {localInputs.current.attachments--;updateReadiness.changed();}
    }
    if (fileInput.current) fileInput.current.value = '';
  }
  const pendingApprovals = activeRuns.filter(r => r.approval).map(r => r.approval!);
  useEffect(() => { if (hud && !peek && (modelOpen || pendingApprovals.length > 0 || pendingQuestionCount > 0 || images.length > 0)) void switchMode(true, true); }, [hud, peek, modelOpen, pendingApprovals.length, pendingQuestionCount, images.length]);
  const composer = <div className="composer-area">
    {failedSubmissions[draftKey] && <SubmissionRecovery submission={failedSubmissions[draftKey]}/>}
    {(pendingAttachments[draftKey] || []).map(item=><div className="attachment-progress" key={item.id} role={item.state==='error'?'alert':'status'}><span>{item.name} · {item.state==='processing'?'Processing…':item.error}</span>{item.state==='error' && <button className="text-button" onClick={()=>setPendingAttachments(previous=>({...previous,[draftKey]:(previous[draftKey] || []).filter(entry=>entry.id!==item.id)}))}>Remove</button>}</div>)}
    {documentAttachments.length > 0 && <div className="attachment-strip">{documentAttachments.map(item=><span className="document-chip" key={item.id}><FileCode2 size={13}/>{item.name}<button aria-label={`Remove ${item.name}`} onClick={()=>setDocuments(prev=>({...prev,[draftKey]:documentAttachments.filter(doc=>doc.id!==item.id)}))}><X size={12}/></button></span>)}</div>}
    <QuestionPanel chatId={chatId} active={running} notify={flash} onPending={setPendingQuestionCount} />
    {pendingApprovals.length > 0 && <div className="approval-stack">{pendingApprovals.map(approval => {const origin=lives[approval.job_id]?.run; return <div className="approval-card" key={approval.approval_id}><div><ShieldCheck size={16} /><strong>Approval needed</strong><span>{approval.name}</span></div><small>{chats.find(item=>item.id===origin?.chat_id)?.title || 'Originating chat'} · {projects.find(item=>item.id===origin?.project_id)?.name || 'Personal workspace'}</small>{origin?.chat_id && origin.chat_id!==chatId && <button className="text-button" onClick={()=>void loadChat(origin.chat_id!)}>Open chat</button>}<details><summary>Action details</summary><pre>{approval.command || JSON.stringify(approval.arguments || {}, null, 2)}</pre></details><footer><button className="subtle" onClick={() => void decide(approval, false)}>Deny</button><button className="primary" onClick={() => void decide(approval, true)}>Allow once</button></footer></div>;})}</div>}
    {notice && (!hud || peek) && <div className="notice" role="status"><span>{notice}</span>{dictationSetupNeeded && <button className="text-button" onClick={() => { setSettingsTab('dictation'); setPage('settings'); setDictationSetupNeeded(false); if (hud) void switchMode(false); }}>Open Dictation settings</button>}<IconButton label="Dismiss notice" onClick={() => setNotice('')}><X size={13} /></IconButton></div>}
    <div className={`composer ${running?'active-work':''}`}>
      {draft.startsWith('/') && !draft.includes(' ') && !slashDismissed && <SlashPalette query={draft} activeIndex={slashIndex} select={selectSlash} />}
      {images.length > 0 && <div className="attachment-strip">{images.map((image, index) => <div key={index}><img src={`data:image/jpeg;base64,${image}`} alt={`Attachment ${index + 1}`} /><IconButton label={`Remove attachment ${index + 1}`} onClick={() => setAttachments(prev => ({ ...prev, [draftKey]: images.filter((_, i) => i !== index) }))}><X size={12} /></IconButton></div>)}</div>}
      <textarea ref={promptRef} aria-label="Message Forge" aria-controls={draft.startsWith('/')&&!draft.includes(' ')&&!slashDismissed?'forge-slash-options':undefined} aria-activedescendant={draft.startsWith('/')&&!draft.includes(' ')&&!slashDismissed?`forge-slash-${slashIndex}`:undefined} placeholder={recording ? 'Listening… click the microphone to finish' : running ? 'Guide the current run…' : 'Ask Forge, or type / for commands'} value={draft} onChange={e => setDraft(e.target.value)} rows={hud ? 1 : 2} maxLength={72000} onKeyDown={e => {if(e.nativeEvent.isComposing)return; const commands=slashCommands.filter(([name])=>name.includes(draft.slice(1).toLowerCase())); if(draft.startsWith('/')&&!draft.includes(' ')&&!slashDismissed&&commands.length){if(e.key==='ArrowDown'||e.key==='ArrowUp'){e.preventDefault();setSlashIndex((slashIndex+(e.key==='ArrowDown'?1:-1)+commands.length)%commands.length);return;}if(e.key==='Escape'){e.preventDefault();setSlashDismissed(true);return;}if(e.key==='Enter'&&!e.shiftKey){const exact=commands.find(([name])=>draft.toLowerCase()===`/${name}`);const selected=exact || commands[slashIndex % commands.length];if(isComposerMode(selected[0]) || !exact){e.preventDefault();selectSlash(`/${selected[0]} `);return;}}} if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); if (running) void steer(); else void send(); } }} />
      <div className="composer-controls"><div className="composer-left"><IconButton label="Attach files" disabled={running} onClick={() => fileInput.current?.click()}><Plus size={19} /></IconButton><IconButton label={captureBusy ? 'Capturing screen' : 'Attach screenshot'} onClick={() => void capture()} disabled={running || captureBusy || !isNative()}><Maximize2 size={16} /></IconButton><ModelPicker models={models} value={settings.model} providerId={String(settings.provider_id || 'ollama')} select={name => void changeSettings({ model: name, adaptive_model_locked: true })} open={modelOpen} setOpen={setModelOpen} refresh={() => void refreshModels()} /><button className="permission-control" title="Permission profile" onClick={() => { setPage('settings'); setSettingsTab('permissions'); if (hud) void switchMode(false); }}><ShieldCheck size={13} /><span>{settings.permission_profile === 'full_access' ? 'Full access' : settings.permission_profile === 'deny_access' ? 'Chat only' : 'Ask'}</span></button></div><div className="composer-right"><IconButton label="Skills and notes" active={rightPanel && panelTab==='context'} onClick={() => {setRightPanel(!(rightPanel && panelTab==='context')); setPanelTab('context'); if(hud) void switchMode(false);}}><Sparkles size={17}/>{Boolean((taskSkills[draftKey] || []).length+(taskSpaces[draftKey] || []).length) && <span className="context-count">{(taskSkills[draftKey] || []).length+(taskSpaces[draftKey] || []).length}</span>}</IconButton><ContextControl settings={settings} onChange={delta=>changeSettings({...delta,adaptive_context_locked:true},true)} open={contextOpen} setOpen={value => { setContextOpen(value); if (value && hud && !peek) void switchMode(true, true); }} /><IconButton label={dictationBusy ? 'Preparing dictation' : transcribing ? 'Cancel transcription' : recording ? 'Stop dictation' : 'Start dictation'} active={recording} disabled={dictationBusy} onClick={() => void dictate()}>{dictationBusy ? <LoaderCircle size={17} className="spin" /> : recording ? <AudioLines size={17} className="pulse" /> : <Mic size={17} />}</IconButton><>{running && <button className="steer-button" title="Guide the current run (Enter)" disabled={busy || !draft.trim()} onClick={() => void steer()}><Waypoints size={14} /><span>{draft.startsWith('/') || commandMode ? 'Command' : 'Steer'}</span></button>}<button className="send-button" aria-label={running ? 'Stop generation' : 'Send message'} title={running ? 'Stop (Shift+Escape)' : 'Send (Enter)'} disabled={!running && (busy || processingAttachments || !draft.trim() || (!settings.model && !draft.startsWith('/')) || !workspaceReady)} onClick={() => running ? void controlRun('cancel') : void send()}>{running ? <Square size={14} fill="currentColor" /> : <ArrowUp size={18} />}</button></></div></div>
    </div>
    {commandMode && <ComposerModeChip mode={commandMode} remove={()=>{if(!updateReadiness.canEdit())return;setCommandModes(previous=>({...previous,[draftKey]:null}));promptRef.current?.focus();}}/>}
    <div className="draft-status"><small>{persistedDraft.status}</small>{persistedDraft.conflict && <button className="text-button" onClick={()=>void persistedDraft.reloadSaved()}>Reload saved draft</button>}{Boolean(commandMode || draft || documentAttachments.length || (taskSkills[draftKey] || []).length || (taskSpaces[draftKey] || []).length) && <button className="text-button" disabled={!workspaceReady || processingAttachments} onClick={()=>void persistedDraft.discard()}>Discard draft</button>}</div><div className="composer-status"><span className={`composer-connection ${running ? 'status-running' : ''}`}><i />{dictationBusy ? 'Loading local dictation…' : live?.status || connection}{(live || lastRun) && <TaskStatus compact run={(live || lastRun)!.run} status={live?.status} notify={flash} resume={()=>void controlRun('resume')} pause={()=>void controlRun('pause')}/>}</span><span className="composer-metrics"><span>{tokenLabel(settings.context)} context</span><span aria-hidden="true">·</span><span className="metric-speed" aria-label="Generation speed in tokens per second">Tokens/s: {speedLabel}</span></span></div>
  </div>;

  return <><div ref={workspaceRoot} data-update-phase={handoff.phase} data-accent={project?.appearance?.accent || uiAppearance.accent} data-motion={uiAppearance.motion} data-visibility={visibleDocument?'visible':'hidden'} style={{'--sidebar-width':`${workspaceLayout.layout.sidebar_width}px`,'--right-panel-width':`${workspaceLayout.layout.right_panel_width}px`} as CSSProperties} className={`app ${hud ? 'hud' : ''} ${peek ? 'peek' : ''} ${!sidebar ? 'sidebar-collapsed' : ''}`}>
    <input ref={fileInput} type="file" hidden accept="image/*,.txt,.md,.pdf,.docx,.csv,.tsv,.xlsx,.json" multiple onChange={e => void attachFiles(e.target.files)} />
    <Suspense fallback={null}>{showSetup && <SetupWizard settings={settings} models={models} onChange={changeSettings} refreshModels={refreshModels} close={() => setShowSetup(false)} openSettings={tab => { setShowSetup(false); setSettingsTab(tab); setPage('settings'); }} />}{showNotifications && <NotificationCenter close={() => setShowNotifications(false)} openRun={id => { const item = liveRef.current[id]; if (item?.run.chat_id) void loadChat(item.run.chat_id); else void api<{ runs: Run[] }>('runs').then(r => { const run = r.runs.find(v => v.id === id); if (run?.chat_id) void loadChat(run.chat_id); }); setShowNotifications(false); }} />}</Suspense>
    {!hud && sidebar && <WorkspaceSidebar workspaceName={uiAppearance.name} accent={uiAppearance.accent} width={workspaceLayout.layout.sidebar_width} onWidth={value=>workspaceLayout.update({sidebar_width:value})} collapsedProjectIds={workspaceLayout.layout.collapsed_project_ids} onCollapsedProjects={ids=>workspaceLayout.update({collapsed_project_ids:ids})} page={page} projects={projects} chats={chats} projectId={projectId} chatId={chatId} search={search} onSearch={setSearch} scheduledCount={scheduledCount} activeChatIds={activeRuns.map(item => item.run.chat_id || '').filter(Boolean)} navItems={navItems} onPage={setPage} onNew={newChat} onCollapse={() => setSidebar(false)} onConnect={() => void registerProject()} onProject={chooseProject} onChat={id => {if(compactViewport)setSmallSidebar(false);void loadChat(id);}} onChatChanged={chatChanged} onProjectRemoved={projectRemoved} />}
    <div className="main-column">
      <header className="workspace-header"><div className="header-left">{!sidebar && !hud && <IconButton label="Open sidebar" onClick={() => setSidebar(true)}><PanelLeftOpen size={17} /></IconButton>}{hud ? <img className="hud-logo" src="./forge.svg" alt="Forge" /> : <><button className="header-project" onClick={() => setPage('projects')}>{project?<ProjectIcon project={project} size={14}/>:<Folder size={14}/>}<span>{project?.name || uiAppearance.name}</span><ChevronDown size={12} /></button><ChevronRight size={12} /><button className="header-title" title={page==='chat'&&chatId?'Rename chat':undefined} disabled={page!=='chat'||!chatId} onClick={openChatRename}>{page === 'chat' ? chat?.title || 'New chat' : page === 'scheduled' ? 'Scheduled' : page === 'plugins' ? 'Library' : page.charAt(0).toUpperCase() + page.slice(1)}</button></>}{project?.branch && <span className="branch-badge"><GitBranch size={12} />{project.branch}</span>}</div><div className="header-right">{hud && notice && !peek && <IconButton label="Show notification" onClick={() => void switchMode(true, true)}><Info size={15} /></IconButton>}{!hud && <IconButton label="Notifications" onClick={() => setShowNotifications(true)}><Bell size={17} /></IconButton>}{hud && <button className="hud-reply-toggle text-button" aria-expanded={peek} onClick={() => void switchMode(true, !peek)}>{peek ? 'Hide reply' : 'Show reply'}</button>}{!hud && isNative() && <IconButton label="Open Forge browser" onClick={() => { setRightPanel(true); setPanelTab('browser'); }}><Globe size={17} /></IconButton>}<IconButton label={hud ? 'Full workspace' : 'Minimal HUD'} onClick={() => void switchMode(!hud)}>{hud ? <Expand size={17} /> : <Minimize2 size={17} />}</IconButton>{!hud && <IconButton label={rightPanel ? 'Close workspace panel' : 'Open workspace panel'} active={rightPanel} onClick={() => setRightPanel(!rightPanel)}>{rightPanel ? <PanelRightClose size={17} /> : <PanelRightOpen size={17} />}</IconButton>}</div></header>
      <div className="workspace-body"><main className={`content ${page !== 'chat' ? 'page-content' : 'chat-content'}`}>
        {page === 'chat' ? <><div className="chat-scroll" ref={chatScroll} onScroll={() => { const el = chatScroll.current; if (el) {shouldScroll.current = el.scrollHeight - el.scrollTop - el.clientHeight < 120;setJumpToLatest(!shouldScroll.current);} }}><div className="chat-width">{!chat?.messages?.length && !live?.text && !live?.thinking ? <div className="welcome"><div className="welcome-ambient" aria-hidden="true"/><div className="welcome-mark"><img src="./forge.svg" alt="" /></div><h1>What are we building?</h1><p>{project?`Working in ${project.name}`:uiAppearance.name==='Forge'?'Your models. Your projects. Your workspace.':uiAppearance.name}</p><div className="welcome-suggestions"><button onClick={() => { setDraft('/plan '); promptRef.current?.focus(); }}><Command size={16} /><span>Plan a change<small>Explore before building</small></span></button><button onClick={() => { setDraft('/goal '); promptRef.current?.focus(); }}><Check size={16} /><span>Start a goal<small>Keep progress through long tasks</small></span></button><button onClick={() => void registerProject()}><FolderPlus size={16} /><span>Open a project<small>Connect a local folder</small></span></button></div></div> : <>{(chat?.total_messages || 0) > (chat?.messages?.length || 0) && <button className="load-earlier" disabled={historyLoading} onClick={() => chatId && void loadChat(chatId, messagesLimit + 80,true)}>{historyLoading?'Loading earlier messages…':'Load earlier messages'}</button>}{chat?.summary && <div className="compaction-note"><Layers3 size={13} />Earlier context compacted · Original messages saved</div>}{unanchoredActivities.map(group=><ChatActivity key={group.run_id} group={group} lives={recentRuns}/>)}{chat?.messages?.map((message, index) => message.role==='tool'&&message.invocation_id&&message.run_id&&activityOwners.has(message.run_id)?null:<div key={message.id || `${chat.id}-${index}`} data-message-id={message.id}><MessageView message={message} showThinking={settings.thinking} thinkingIntent={savedThinking.get(String(message.id))}/>{activitiesFor(message.id).map(group=><ChatActivity key={group.run_id} group={group} lives={recentRuns}/>)}</div>)}{live && !live.finished && <>{visibleLive?.parts.map((message, i) => <MessageView key={`part-${i}`} message={message} showThinking={settings.thinking} thinkingIntent={liveThinking?.parts[visiblePartOffset + i]} />)}{visibleLive?.showCurrent && (live.text || live.thinking) && <MessageView message={{ role: 'assistant', content: live.text, thinking: live.thinking }} streaming showThinking={settings.thinking} thinkingIntent={liveThinking?.current} />}</>}{readyPlan && !running && <div className="plan-actions"><div><Badge>Plan ready</Badge><span>{readyPlan.tasks.length} steps saved</span><small className="goal-build-hint">Build starts a tracked goal</small><button className="text-button" onClick={() => setPlanPreview(true)}>Review plan</button></div><button className="primary" disabled={busy} onClick={() => void buildPlan()}><Play size={13} />Build</button></div>}</>}</div></div>{jumpToLatest && <button className="jump-to-latest" onClick={()=>{shouldScroll.current=true;if(chatScroll.current)chatScroll.current.scrollTop=chatScroll.current.scrollHeight;setJumpToLatest(false);}}><ArrowUp size={14} className="arrow-down"/>Jump to latest</button>}{composer}</> : <div className="page-scroll">{page === 'builder' && <Suspense fallback={<p className="muted">Loading Builder…</p>}><BuilderPage projects={projects} chats={chats} runs={Object.values(lives).map(item=>item.run)} onRun={run=>trackRun(run,false)} onOpenChat={id=>void loadChat(id)} notify={flash}/></Suspense>}{page === 'projects' && <ProjectsPage projects={projects} selected={projectId} open={chooseProject} refresh={refreshWorkspace} notify={flash} />}{page === 'spaces' && <SpacesPage projects={projects} notify={flash} />}{page === 'scheduled' && <SchedulesPage projects={projects} notify={flash} onRun={trackRun} onCount={setScheduledCount} />}{page === 'agents' && <AgentsPage models={models} projects={projects} projectId={projectId} settings={settings} notify={flash} onRun={trackRun} />}{page === 'plugins' && <Suspense fallback={<p className="muted">Loading library…</p>}><PluginsPage notify={flash} projectId={projectId} /></Suspense>}{page === 'usage' && <UsagePage projects={projects} models={models} timezone={settings.timezone} />}{page === 'memory' && <Suspense fallback={<p className="muted">Loading memory…</p>}><MemoryPage projects={projects} settings={settings} onChange={changeSettings} notify={flash} /></Suspense>}{page === 'settings' && <SettingsPageLoader load={loadSettingsPage} onReturn={returnToChat} pageProps={{settings,onChange:changeSettings,onContextChange:delta=>changeSettings({...delta,adaptive_context_locked:true},true),applyRecommendation,models,refreshModels,tab:settingsTab,setTab:setSettingsTab,capabilities,openSetup:() => setShowSetup(true),notify:flash}} />}</div>}
      </main>{rightPanel && !hud && <WorkspacePanel tab={panelTab} select={setPanelTab} close={()=>setRightPanel(false)} width={workspaceLayout.layout.right_panel_width} resize={value=>workspaceLayout.update({right_panel_width:value})}>
        <Suspense fallback={<p className="muted">Loading browser…</p>}>{panelTab === 'browser' && <BrowserPanel />}</Suspense>
        {panelTab === 'context' && <TaskContextPanel projectId={projectId} skills={taskSkills[draftKey] || []} notes={taskSpaces[draftKey] || []} spaces={spaces} onSkills={value=>setTaskSkills(prev=>({...prev,[draftKey]:value}))} onNotes={value=>setTaskSpaces(prev=>({...prev,[draftKey]:value}))}/>}
        {panelTab === 'activity' && <WorkspaceActivity lives={recentRuns} scope={{page,chatId,projectId,chat,chats}} notify={flash} resume={id=>void controlRun('resume',id)}/>}
        {panelTab === 'goals' && <GoalsPanel projectId={projectId} chats={chats} version={goalVersion} notify={flash} onRun={trackRun} />}
        {panelTab === 'files' && <FilePanel projectId={projectId} notify={flash} />}
      </WorkspacePanel>}</div>
      {!hud && page !== 'chat' && notice && <div className="global-notice" role="status">{notice}<IconButton label="Dismiss notice" onClick={() => setNotice('')}><X size={14} /></IconButton></div>}
    </div>
    {planPreview && readyPlan && <Modal title="Implementation plan" wide close={() => setPlanPreview(false)}><p className="muted">{readyPlan.request}</p><Markdown text={readyPlan.markdown} />{readyPlan.path && <small className="plan-file-path">{readyPlan.path}</small>}<footer><button className="subtle" onClick={() => setPlanPreview(false)}>Close</button><button className="primary" disabled={busy || running} onClick={() => void buildPlan()}><Play size={14} />Build this plan</button></footer></Modal>}
    {rename && <Modal title="Rename conversation" close={closeChatRename}><form onSubmit={e=>{e.preventDefault();void saveChatRename();}}><input autoFocus aria-label="Conversation title" value={renameValue} maxLength={200} disabled={renameBusy} onChange={e=>setRenameValue(e.target.value)}/><footer><button className="primary" disabled={renameBusy || !renameValue.trim()}>{renameBusy?'Saving…':'Save'}</button></footer></form></Modal>}
    {authNeeded && <Modal title="Connect to your coordinator" close={() => setAuthNeeded(false)}><p className="muted">Generate a pairing code in the Forge desktop app under Settings → Browser, then enter it here.</p><form onSubmit={e => { e.preventDefault(); void api('pair', { code: authInput }).then(() => { setAuthInput(''); booted.current = false; void bootstrap(); }).catch(e => flash(errorText(e))); }}><input type="text" autoComplete="off" autoFocus aria-label="Browser pairing code" value={authInput} onChange={e => setAuthInput(e.target.value)} /><footer><button className="primary">Connect</button></footer></form></Modal>}
  </div><UpdateHandoffSurface layoutRecovery={{error:workspaceLayout.error,retrying:workspaceLayout.retrying,retry:workspaceLayout.retry}}/></>;
}

export function FilePanel({ projectId, notify }: { projectId: string | null; notify: (message: string) => void }) {
  const [path, setPath] = useState(''); const [files, setFiles] = useState<{ name: string; path?: string; is_dir?: boolean; type?: string }[]>([]); const [preview, setPreview] = useState(''); const [previewName, setPreviewName] = useState(''); const [error, setError] = useState('');
  const readGeneration=useRef(0), readScope=useRef('');const scope=`${projectId || ''}:${path}`;if(readScope.current!==scope){readScope.current=scope;readGeneration.current++;}
  useEffect(()=>()=>{readGeneration.current++;},[]);
  useEffect(() => { setPath(''); setPreview(''); setPreviewName(''); setError(''); }, [projectId]);
  useEffect(() => { let disposed = false; setFiles([]); setPreview(''); if (!projectId) return; void api<{ entries?: typeof files; files?: typeof files }>('workspace_files', { project_id: projectId, path: path || '.' }).then(r => { if (disposed) return; setFiles(r.entries || r.files || []); setError(''); }).catch(e => { if (!disposed) setError(errorText(e)); }); return () => { disposed = true; }; }, [projectId, path]);
  async function open(file: typeof files[number]) { const name = file.path || `${path ? path + '/' : ''}${file.name}`; if (file.is_dir || file.type === 'directory') { setPath(name); return; }const request=++readGeneration.current,captured=readScope.current;try { const result = await api<{ content: string }>('workspace_read', { project_id: projectId, path: name });if(request!==readGeneration.current || captured!==readScope.current)return;setPreview(result.content); setPreviewName(name); } catch (e) {if(request===readGeneration.current && captured===readScope.current)notify(errorText(e));} }
  if (!projectId) return <Empty icon={<Folder size={22} />} title="Connect a project" text="Browse files after selecting a project folder." />;
  return <div className="file-panel">{previewName ? <><button className="text-button" onClick={() => setPreviewName('')}>← Files</button><h3>{previewName}</h3><pre>{preview}</pre></> : <><div className="file-path"><button className="text-button" onClick={() => setPath('')}>Project</button><span>{path}</span></div>{path && <button className="file-row" onClick={() => setPath(path.split('/').slice(0, -1).join('/'))}><Folder size={14} />..</button>}{error && <div className="inline-error">{error}</div>}{files.map(file => <button className="file-row" key={file.name} onClick={() => void open(file)}>{file.is_dir || file.type === 'directory' ? <Folder size={14} /> : <FileCode2 size={14} />}<span>{file.name}</span></button>)}</>}</div>;
}

export { applyEvents, reconcileRunMessages, thinkingReadOwners };
