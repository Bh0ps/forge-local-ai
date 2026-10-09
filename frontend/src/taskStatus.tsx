import { useCallback, useState } from 'react';
import { Info, Pause, Play, ShieldAlert } from 'lucide-react';
import { Badge } from './components';
import { RecoveryPanel, type RecoveryInspection } from './recoveryPanel';
import type { PlannerAssignment, Run } from './types';

export function GuidanceCard({ assignment }: { assignment?: PlannerAssignment }) {
  if (!assignment) return <p className="guidance-state muted">OpenRouter guidance has not been requested for this scope.</p>;
  const label = {requested:'New guidance requested · Resume to run the planner',waiting:'Planning requested',consumed:'Guidance supplied to local execution',unavailable:'Guidance unavailable · continuing locally',superseded:'Previous guidance superseded'}[assignment.state];
  return <div className="guidance-state"><strong>OpenRouter: {label}</strong>{assignment.reason && <small>{assignment.reason}</small>}<small>{assignment.consumed_at ? `Supplied ${new Date(assignment.consumed_at).toLocaleString()}` : assignment.requested_at ? `Requested ${new Date(assignment.requested_at).toLocaleString()}` : ''}{assignment.scope_revision !== undefined ? ` · scope ${assignment.scope_revision}` : ''}</small>{assignment.run_id && <details><summary>Guidance evidence</summary><small>Helper {assignment.run_id} · {assignment.guidance_hash ? `guidance ${assignment.guidance_hash.slice(0,12)}` : 'No supplied result recorded'}</small></details>}</div>;
}
type TaskStatusProps = {run: Run; status?: string; notify: (value:string)=>void; resume:()=>void; pause:()=>void; compact?: boolean};
export function TaskStatus(props: TaskStatusProps) {
  const identity = `${props.run.id}:${props.run.status}:${props.run.progress_summary?.available_actions?.join(',') || ''}`;
  return <TaskStatusContent key={identity} {...props}/>;
}
function TaskStatusContent({run,status,notify,resume,pause,compact}: TaskStatusProps) {
  const [inspection, setInspection] = useState<RecoveryInspection | null>(null);
  const [details, setDetails] = useState(false);
  const inspected = useCallback((value: RecoveryInspection) => setInspection(value), []);
  const summary=run.progress_summary, stage=summary?.phase || run.workflow_stage;
  const stopped=['paused','interrupted','failed'].includes(run.status || '');
  const activity=summary?.activity || (stage ? `${stage.charAt(0).toUpperCase()+stage.slice(1)} task` : status || run.status || 'Working');
  const verified=summary?.verified_count ?? summary?.verified, total=summary?.requirement_count ?? summary?.total;
  const needsInspection = !inspection || inspection.run_id !== run.id || inspection.state !== 'ready' || inspection.unknown_count > 0;
  if (compact) return <span className="compact-run-controls">
    {stopped ? <button className="icon-button" aria-label="Resume" title={needsInspection ? 'Inspect interrupted actions before resuming' : 'Resume run'} disabled={needsInspection} onClick={resume}><Play size={13}/></button> : !['completed','cancelled'].includes(run.status || '') && <button className="icon-button" aria-label="Pause run" title="Pause run" onClick={pause}><Pause size={13}/></button>}
    {(stopped || summary?.blocker || run.recovery) && <button className="icon-button" aria-label="Task details and recovery" title="Task details and recovery" aria-expanded={details} onClick={() => setDetails(value => !value)}>{needsInspection && stopped ? <ShieldAlert size={13}/> : <Info size={13}/>}</button>}
    {(stopped || run.recovery) && <span className="compact-recovery"><RecoveryPanel runId={run.id} recovery={run.recovery} notify={notify} onInspectionChange={inspected} compact expanded={details}/></span>}
    {details && <span className="compact-task-details"><strong>{activity}</strong>{summary?.blocker && <span>{summary.blocker}</span>}{summary?.next_action && <span>{summary.next_action}</span>}<GuidanceCard assignment={run.planner_assignment}/></span>}
  </span>;
  return <section className="task-status" aria-label="Task status"><div className="task-status-main"><div role="status" aria-live="polite" aria-atomic="true"><strong>{activity}</strong>{total !== undefined && <span>{verified || 0} / {total} requirements verified</span>}{summary?.waiting_reason && <p>{summary.waiting_reason}</p>}{summary?.blocker && <p>{summary.blocker}</p>}</div><Badge>{run.status || 'running'}</Badge>{stopped ? <button className="secondary" disabled={needsInspection} onClick={resume}>Resume</button> : !['completed','cancelled'].includes(run.status || '') && <button className="text-button" onClick={pause}>Pause</button>}</div>{summary?.next_action && <small>Next: {summary.next_action}</small>}{(stopped || run.recovery) && <RecoveryPanel key={run.id} runId={run.id} recovery={run.recovery} notify={notify} onInspectionChange={inspected}/>}<GuidanceCard assignment={run.planner_assignment}/>{run.review && <small>Independent review: {run.review.status.replaceAll('_',' ')}{run.review.summary ? ` · ${run.review.summary}` : ''}</small>}</section>;
}
