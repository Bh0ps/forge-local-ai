import { useEffect, useRef, useState } from 'react';
import { Activity, Bot, ChevronDown, ChevronRight, LoaderCircle } from 'lucide-react';
import { api, errorText, friendlyModel } from './api';
import { ActivityCard, DiagnosticsExport } from './runInspector';
import type { ActivityGroup, Run, RunEvent, SavedChat } from './types';

export const RUN_ACTIVE=new Set(['queued','running','compacting','reviewing','waiting_approval','awaiting_approval','waiting_question','awaiting_question','starting','resuming']);
const visibleEvents=new Set(['tool','outcome_unknown','specialist','verification','skill','error','approval','question','question_answered','steer','goal_review']);
export interface ActivityLive {run:Run;tools:RunEvent[];status:string;finished:boolean;}
export function latestActivityAssignments(groups:ActivityGroup[]):ActivityGroup[] {
  const latest=new Map<string,ActivityGroup>();
  for(const group of groups){const key=group.assignment_id || group.run_id, previous=latest.get(key);if(!previous){latest.set(key,group);continue;}const newer=(group.attempt || 1)>(previous.attempt || 1) || (group.attempt || 1)===(previous.attempt || 1) && (Date.parse(group.updated_at || group.created_at || '')||0)>=(Date.parse(previous.updated_at || previous.created_at || '')||0);const chosen=newer?group:previous;const history=new Map([...previous.attempts || [],...group.attempts || [],previous,group].filter(item=>item.run_id!==chosen.run_id).map(item=>[item.run_id,{...item,attempts:undefined}]));latest.set(key,{...chosen,...(history.size?{attempts:[...history.values()].sort((a,b)=>(a.attempt || 1)-(b.attempt || 1))}:{})});}
  return [...latest.values()];
}
export function mergeActivityEvents(saved:RunEvent[],incoming:RunEvent[]):RunEvent[] {
  const result=[...saved];
  for(const event of incoming){if(!visibleEvents.has(event.type))continue;const run=event.run_id || '';const index=result.findIndex(previous=>(previous.run_id || '')===run && (event.invocation_id?previous.invocation_id===event.invocation_id:event.seq!==undefined && previous.seq===event.seq));
    if(index<0)result.push(event);else if((Number(event.seq)||0)>=(Number(result[index].seq)||0))result[index]={...result[index],...event};
  }
  return result.sort((a,b)=>(Number(a.seq)||0)-(Number(b.seq)||0));
}
export function transcriptActivity(chat:SavedChat|null,lives:ActivityLive[]):ActivityGroup[] {
  const saved=chat?.activity_groups || [], byId=new Map(lives.map(item=>[item.run.id,item]));
  const build=(group:ActivityGroup):ActivityGroup=>{const live=byId.get(group.run_id);const persisted=group.children || [];const known=new Set(persisted.map(child=>child.run_id));const children=latestActivityAssignments([...persisted,...lives.filter(item=>item.run.parent_id===group.run_id && !known.has(item.run.id)).map(item=>fromLive(item))]).map(build);
    const tools=live?new Set(live.tools.filter(event=>event.type==='tool'||event.type==='outcome_unknown').map(event=>event.invocation_id || event.seq)).size:0;
    const attempts=(group.attempts || []).map(build);
    const liveSummary=live?.run.progress_summary?.blocker || live?.run.progress_summary?.waiting_reason;
    const finished=live?.run.status==='completed' || live?.run.status==='cancelled';
    return {...group,...(live?{status:live.run.status || group.status,current_step:live.run.progress_summary?.activity || live.status,model:live.run.returned_model_last || live.run.continuation_model || group.model || live.run.model,provider_id:String(live.run.settings?.provider_id || group.provider_id || ''),summary:liveSummary || (finished?'':group.summary),failure:live.run.failure===undefined?group.failure:live.run.failure || undefined,finish_reason:live.run.last_finish_reason || group.finish_reason,response_provider:live.run.response_provider || group.response_provider}:{}),tool_count:Math.max(group.tool_count || 0,tools+attempts.reduce((total,item)=>total+item.tool_count,0)),helper_count:Math.max(group.helper_count || 0,children.length),children,attempts};};
  const fromLive=(item:ActivityLive):ActivityGroup=>({run_id:item.run.id,request_message_id:item.run.request_message_id,status:item.run.status || 'running',mode:item.run.mode,model:item.run.returned_model_last || item.run.continuation_model || item.run.model,provider_id:String(item.run.settings?.provider_id || ''),created_at:item.run.created_at,current_step:item.status,tool_count:0,helper_count:0,children:[],assignment_id:item.run.assignment_id,attempt:item.run.attempt,retry_count:item.run.retry_count,failure:item.run.failure || undefined,finish_reason:item.run.last_finish_reason,response_provider:item.run.response_provider});
  const seen=new Set(saved.map(group=>group.run_id));
  return [...saved,...lives.filter(item=>item.run.chat_id===chat?.id && !item.run.parent_id && !seen.has(item.run.id)).map(fromLive)].map(build).sort((a,b)=>(a.request_message_id || 0)-(b.request_message_id || 0));
}
export function activityRunIds(groups:ActivityGroup[]):Set<string>{return new Set(groups.flatMap(group=>[group.run_id,...activityRunIds(group.children || []),...activityRunIds(group.attempts || [])]));}
interface ActivityResult {run_id:string;events:RunEvent[];next_cursor:number;has_more:boolean;status:string;children?:ActivityGroup[];}
export function activityFailureLabel(failure:ActivityGroup['failure']):string {
  if(typeof failure==='string')return failure;if(!failure || typeof failure!=='object')return '';
  const continuation=continuationFailureLabel(failure);if(continuation)return continuation;
  const labels:Record<string,string>={protocol:'Incomplete or invalid tool response',tool_protocol:'Incomplete or invalid tool response',continuation_unavailable:'The selected model could not continue the tool exchange',empty_result:'The helper returned no visible findings',timeout:'Provider response timed out',context:'The request exceeded the model context',refusal:'The provider declined this request',configuration:'Provider configuration needs attention',cancelled:'The helper was cancelled'};
  return `${failure.provider_id || 'Provider'}: ${labels[failure.category || ''] || String(failure.category || 'Request failed').replaceAll('_',' ')}${failure.http_status?` (HTTP ${failure.http_status})`:''}`;
}
function continuationFailureLabel(failure:ActivityGroup['failure']):string {
  if(!failure || typeof failure!=='object' || failure.category!=='continuation_unavailable')return '';
  const requested=failure.requested_context,limit=failure.model_context_limit;
  if(failure.continuation_reason==='context_limit' && typeof requested==='number' && typeof limit==='number'
    && Number.isSafeInteger(requested) && Number.isSafeInteger(limit)
    && requested>0 && requested<=262144 && limit>0 && limit<=16777216 && requested>limit)
    return `This route supports ${limit.toLocaleString('en-US')} tokens; the helper requests ${requested.toLocaleString('en-US')}. No tool from this response ran.`;
  const reasons:Record<string,string>={model_unavailable:'The returned model is not advertised as an exact free model.',capabilities:'The returned model does not advertise the capabilities this helper needs.',model_changed:'The provider changed models during this tool exchange.'};
  return reasons[failure.continuation_reason || ''] ? `${reasons[failure.continuation_reason!]} No tool from this response ran.` : '';
}
function ActivityStep({event}: {event:RunEvent}) {
  if(['tool','outcome_unknown','specialist','verification','skill'].includes(event.type))return <ActivityCard event={event}/>;
  const labels:Record<string,string>={error:'Task needs attention',approval:'Approval requested',question:'Question requested',question_answered:'Answers received',steer:'Direction received',goal_review:'Independent review'};
  const review=typeof event.review==='object'&&event.review?event.review as {summary?:string;status?:string}:undefined;
  const detail=event.text || String(event.reason || review?.summary || event.state || review?.status || '');
  return <article className="activity-event"><strong>{labels[event.type] || event.type.replaceAll('_',' ')}</strong>{detail && <p>{detail}</p>}</article>;
}
export function ChatActivity({group,lives=[],helper=false}: {group:ActivityGroup;lives?:ActivityLive[];helper?:boolean}) {
  const [open,setOpen]=useState(false), [events,setEvents]=useState<RunEvent[]>([]), [children,setChildren]=useState<ActivityGroup[]>([]), [loading,setLoading]=useState(false), [error,setError]=useState(''), [hasMore,setHasMore]=useState(false), [retry,setRetry]=useState(0);
  const cursor=useRef(0), pending=useRef(false), generation=useRef(0), current=useRef(group.run_id);current.current=group.run_id;
  const live=lives.find(item=>item.run.id===group.run_id), active=RUN_ACTIVE.has(group.status), liveEvents=(live?.tools || []).map(event=>({...event,run_id:group.run_id}));
  const shown=mergeActivityEvents(events,liveEvents), nested=transcriptActivity({id:'',title:'',project_id:null,model:'',messages:[],activity_groups:latestActivityAssignments([...(group.children || []),...children])},lives);
  // Detail reads never replay model text or control a run; identity is journal seq/invocation ID.
  async function load(){if(pending.current)return;pending.current=true;setLoading(true);const request=generation.current,id=group.run_id;try{const result=await api<ActivityResult>('run_activity',{run_id:id,after:cursor.current,limit:100});if(request!==generation.current || current.current!==id)return;setEvents(previous=>mergeActivityEvents(previous,(result.events || []).map(event=>({...event,run_id:id}))));cursor.current=Math.max(cursor.current,result.next_cursor || 0);setHasMore(Boolean(result.has_more));setChildren(result.children || []);setError('');}catch(failure){if(request===generation.current)setError(errorText(failure));}finally{if(request===generation.current){pending.current=false;setLoading(false);}}}
  useEffect(()=>{generation.current++;cursor.current=0;pending.current=false;setEvents([]);setChildren([]);setError('');setHasMore(false);return()=>{generation.current++;};},[group.run_id]);
  useEffect(()=>{if(!open)return;void load();if(!active)return;const timer=setInterval(()=>{if(document.visibilityState!=='hidden')void load();},2500);return()=>clearInterval(timer);},[open,group.run_id,active,retry]);
  const label=helper?'Helper activity':'Task activity', status=group.status.replaceAll('_',' ');
  const outcome=continuationFailureLabel(group.failure) || group.summary || activityFailureLabel(group.failure);
  return <section className={`chat-activity ${helper?'helper-activity':''} ${active?'active-work':''}`} aria-label={label}>
    <button type="button" className="chat-activity-summary" aria-expanded={open} onClick={()=>setOpen(!open)}>{open?<ChevronDown size={14}/>:<ChevronRight size={14}/>}<span className="activity-mark">{helper?<Bot size={15}/>:active?<LoaderCircle className="spin" size={15}/>:<Activity size={15}/>}</span><span className="activity-title"><strong>{group.current_step || (active?'Working':status)}</strong><small>{helper && group.provider_id?`${group.provider_id} · `:''}{helper && group.model?`${friendlyModel(group.model)} · `:''}{helper && (group.attempt || 1)>1?`Attempt ${group.attempt} · `:''}{group.tool_count || 0} tool {group.tool_count===1?'step':'steps'}{group.helper_count?` · ${group.helper_count} ${group.helper_count===1?'helper':'helpers'}`:''}</small></span><span className="activity-state">{status}</span></button>
    {outcome && (!group.conversation_removed || open) && <p className="activity-outcome">{outcome}</p>}
    {open && <div className="chat-activity-detail">{group.conversation_removed && <small>Helper conversation removed · {group.summary?'findings retained in this activity.':'result status retained here.'}{group.summary_truncated?' The retained summary is shortened.':''}</small>}{error && <p role="alert">{error}<button className="text-button" onClick={()=>setRetry(value=>value+1)}>Retry activity</button></p>}{loading && !shown.length && <small role="status">Loading saved activity…</small>}{shown.map(event=><ActivityStep key={event.invocation_id?`invocation:${group.run_id}:${event.invocation_id}`:`event:${group.run_id}:${event.seq}`} event={event.type==='outcome_unknown'?{...event,state:'Outcome unknown'}:event}/>)}{!loading && !error && !hasMore && !shown.length && <small>{group.conversation_removed?'Temporary tool details were removed with this helper conversation.':'No detailed activity was recorded for this task.'}</small>}{hasMore && <button className="text-button" disabled={loading} onClick={()=>void load()}>Load more activity</button>}{nested.map(child=><ChatActivity key={child.run_id} group={child} lives={lives} helper/>)}<DiagnosticsExport runId={group.run_id}/>{Boolean(group.attempts?.length) && <details className="activity-attempts"><summary>Earlier attempts ({group.attempts!.length})</summary>{group.attempts!.map(attempt=><ChatActivity key={attempt.run_id} group={attempt} lives={lives} helper/>)}</details>}</div>}
  </section>;
}
