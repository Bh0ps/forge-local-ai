import { useId, useLayoutEffect, useRef, useState } from 'react';
import { Activity, Files, Globe, ListChecks, Sparkles, X } from 'lucide-react';
import { Empty, IconButton } from './components';
import { activityRunIds, ChatActivity, latestActivityAssignments, transcriptActivity, type ActivityLive } from './chatActivity';
import { GoalReviewCard } from './goalReviewCard';
import { RecoveryPanel, type RecoveryInspection } from './recoveryPanel';
import { ResizeHandle } from './workspaceLayout';
import type { ActivityGroup, Chat, Run, SavedChat } from './types';
import './workspacePanel.css';

export const WORKSPACE_TABS = [
  { id: 'context', title: 'Context', description: 'Skills and notes for your next message.', icon: Sparkles },
  { id: 'browser', title: 'Browser', description: 'Browse alongside your agent.', icon: Globe },
  { id: 'activity', title: 'Activity', description: 'Saved tool steps and helper results.', icon: Activity },
  { id: 'goals', title: 'Goals', description: 'Requirements, progress and verified results.', icon: ListChecks },
  { id: 'files', title: 'Files', description: 'Explore the current project.', icon: Files },
] as const;

export function WorkspacePanel({ tab, select, close, width, resize, children }: {
  tab: string; select: (tab: string) => void; close: () => void; width: number; resize: (width: number) => void; children: React.ReactNode;
}) {
  const current = WORKSPACE_TABS.find(item => item.id === tab) || WORKSPACE_TABS[2];
  const id = useId(), buttons = useRef(new Map<string, HTMLButtonElement>()), keyboardFocus = useRef(false);
  useLayoutEffect(() => {
    if (keyboardFocus.current) { buttons.current.get(current.id)?.focus(); keyboardFocus.current = false; }
  }, [current.id]);
  return <aside aria-label="Workspace details" style={{ width }} className={`right-panel workspace-panel${current.id === 'browser' ? ' with-browser' : ''}`}>
    <ResizeHandle label="Resize workspace panel" value={width} min={280} max={520} change={resize} reverse />
    <header className="workspace-panel-heading"><div><h2>{current.title}</h2><p>{current.description}</p></div><IconButton label="Close detail pane" onClick={() => {close();queueMicrotask(()=>document.querySelector<HTMLButtonElement>('button[aria-label="Open workspace panel"]')?.focus());}}><X size={16} /></IconButton></header>
    <nav className="workspace-panel-tabs" role="tablist" aria-label="Detail pane views">{WORKSPACE_TABS.map((item, index) => <button
      type="button" key={item.id} ref={element => { if (element) buttons.current.set(item.id, element); else buttons.current.delete(item.id); }}
      role="tab" id={`${id}-${item.id}`} aria-controls={`${id}-content`} aria-selected={current.id === item.id} aria-label={item.title} title={item.title} tabIndex={current.id === item.id ? 0 : -1}
      onClick={() => { keyboardFocus.current = false; select(item.id); }} onKeyDown={event => {
        const nextIndex = event.key === 'ArrowRight' ? (index + 1) % WORKSPACE_TABS.length : event.key === 'ArrowLeft' ? (index + WORKSPACE_TABS.length - 1) % WORKSPACE_TABS.length : event.key === 'Home' ? 0 : event.key === 'End' ? WORKSPACE_TABS.length - 1 : -1;
        if (nextIndex < 0) return;
        event.preventDefault();
        const next = WORKSPACE_TABS[nextIndex];
        keyboardFocus.current = next.id !== current.id;
        select(next.id); buttons.current.get(next.id)?.focus();
      }}><item.icon size={17} aria-hidden="true" /></button>)}</nav>
    <div id={`${id}-content`} role="tabpanel" aria-labelledby={`${id}-${current.id}`} className={`right-scroll${current.id === 'browser' ? ' browser-scroll' : ''}`}>{children}</div>
  </aside>;
}

export interface WorkspaceActivityScope { page: string; chatId: string | null; projectId: string | null; chat: SavedChat | null; chats: Chat[]; }

function savedActivity(scope: WorkspaceActivityScope): ActivityGroup[] {
  return scope.chat && (scope.page === 'chat' ? scope.chat.id === scope.chatId : (scope.chat.project_id || null) === scope.projectId) ? scope.chat.activity_groups || [] : [];
}

/** Scope by the ancestor assignment: a helper can have its own chat or worktree. */
export function scopeWorkspaceRuns(lives: ActivityLive[], scope: WorkspaceActivityScope): ActivityLive[] {
  const byId = new Map(lives.map(item => [item.run.id, item])), savedIds = activityRunIds(savedActivity(scope));
  const chats = new Map(scope.chats.map(chat => [chat.id, chat.project_id || null]));
  return lives.filter(item => {
    let ancestor = item;
    const visited = new Set<string>();
    while (ancestor.run.parent_id && byId.has(ancestor.run.parent_id)) {
      if (visited.has(ancestor.run.id)) return false;
      visited.add(ancestor.run.id); ancestor = byId.get(ancestor.run.parent_id)!;
    }
    if (savedIds.has(ancestor.run.id) || Boolean(ancestor.run.parent_id && savedIds.has(ancestor.run.parent_id))) return true;
    if (scope.page === 'chat') return Boolean(scope.chatId && ancestor.run.chat_id === scope.chatId);
    if (ancestor.run.project_id === undefined && ancestor.run.chat_id && !chats.has(ancestor.run.chat_id)) return false;
    const project = ancestor.run.project_id !== undefined ? ancestor.run.project_id || null : ancestor.run.chat_id ? chats.get(ancestor.run.chat_id) || null : null;
    return project === scope.projectId;
  });
}

export function workspaceActivityGroups(lives: ActivityLive[], scope: WorkspaceActivityScope): ActivityGroup[] {
  const scoped = scopeWorkspaceRuns(lives, scope), saved = savedActivity(scope), savedIds = activityRunIds(saved), ids = new Set(scoped.map(item => item.run.id));
  const additional = scoped.filter(item => !savedIds.has(item.run.id) && (!item.run.parent_id || !ids.has(item.run.parent_id) && !savedIds.has(item.run.parent_id))).map(item => ({
    run_id: item.run.id, request_message_id: item.run.request_message_id, status: item.run.status || 'running', mode: item.run.mode,
    model: item.run.returned_model_last || item.run.continuation_model || item.run.model, provider_id: String(item.run.settings?.provider_id || ''),
    created_at: item.run.created_at, current_step: item.status, tool_count: 0, helper_count: 0, children: [],
    assignment_id: item.run.assignment_id, attempt: item.run.attempt, retry_count: item.run.retry_count,
  }));
  const virtualChat: SavedChat = { id: '__workspace_activity__', title: '', model: '', project_id: scope.projectId, messages: [], activity_groups: latestActivityAssignments([...saved, ...additional]) };
  return transcriptActivity(virtualChat, scoped).sort((a, b) => (Date.parse(b.created_at || '') || 0) - (Date.parse(a.created_at || '') || 0) || (b.request_message_id || 0) - (a.request_message_id || 0));
}

function currentGroupRuns(group: ActivityGroup): ActivityGroup[] { return [group, ...(group.children || []).flatMap(currentGroupRuns)]; }
const RECOVERABLE = new Set(['paused', 'interrupted', 'failed']);

function WorkspaceRunRecovery({ run, helper, notify, resume }: { run: Run; helper: boolean; notify: (message: string) => void; resume: (id: string) => void }) {
  const [inspection, setInspection] = useState<RecoveryInspection | null>(null);
  const blocked = RECOVERABLE.has(run.status || '');
  const canResume = inspection?.run_id === run.id && inspection.state === 'ready' && inspection.unknown_count === 0;
  return <div className="workspace-run-recovery" aria-label={helper ? 'Helper recovery' : 'Task recovery'}>
    <GoalReviewCard review={run.review} paused={blocked} />
    {blocked && <><strong>{helper ? 'Helper needs attention' : 'Task needs attention'}</strong><RecoveryPanel runId={run.id} recovery={run.recovery} notify={notify} onInspectionChange={setInspection} />
      <button className="secondary" disabled={!canResume} title={canResume ? 'Resume task' : 'Inspect interrupted actions before resuming'} onClick={() => resume(run.id)}>Resume</button></>}
  </div>;
}

export function WorkspaceActivity({ lives, scope, notify, resume }: { lives: ActivityLive[]; scope: WorkspaceActivityScope; notify: (message: string) => void; resume: (id: string) => void }) {
  const scoped = scopeWorkspaceRuns(lives, scope), groups = workspaceActivityGroups(lives, scope), byId = new Map(scoped.map(item => [item.run.id, item.run]));
  const [limit, setLimit] = useState(12);
  const needsAttention = (group: ActivityGroup) => currentGroupRuns(group).some(item => RECOVERABLE.has(item.status));
  const visible = groups.filter((group, index) => index < limit || needsAttention(group));
  const hiddenCount = groups.length - visible.length;
  return <section className="workspace-activity" aria-label="Workspace activity">
    {!groups.length && <Empty icon={<Activity size={22} />} title="No activity yet" text={`Tool steps and helper results for ${scope.page === 'chat' ? 'this chat' : 'this project'} appear here.`} />}
    {visible.map(group => <div className="workspace-activity-group" key={group.run_id}><ChatActivity group={group} lives={scoped} />{currentGroupRuns(group).map(item => {
      const live = byId.get(item.run_id), run = {...live,id:item.run_id,status:live?.status || item.status};
      return RECOVERABLE.has(item.status) || run.review && !['completed','cancelled'].includes(run.status) ? <WorkspaceRunRecovery key={`${run.id}:${run.status}`} run={run} helper={item.run_id !== group.run_id} notify={notify} resume={resume} /> : null;
    })}</div>)}
    {hiddenCount > 0 && <button type="button" className="text-button workspace-activity-more" onClick={() => setLimit(value => value + 12)}>Show earlier activity ({hiddenCount})</button>}
  </section>;
}
