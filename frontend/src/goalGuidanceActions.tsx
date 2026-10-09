import { useEffect, useRef, useState } from 'react';
import { api, errorText } from './api';
import { ErrorNotice } from './components';
import type { Goal } from './types';

export function GoalGuidanceActions({goal,conflict,onRequested,notify}: {goal:Goal;conflict:boolean;onRequested:(goal:Goal)=>void;notify:(text:string)=>void}) {
  const requests=useRef(new Map<string,string>()),[busy,setBusy]=useState(false),[error,setError]=useState('');
  const current=useRef({id:goal.id,run_id:goal.run_id});current.current={id:goal.id,run_id:goal.run_id};
  useEffect(()=>setError(''),[goal.id,goal.run_id]);
  if(!goal.run_id || !Number.isInteger(goal.revision) || !['paused','interrupted','failed'].includes(goal.status))return null;
  async function request(){if(busy||conflict||!goal.run_id||goal.revision===undefined)return;const captured={id:goal.id,run_id:goal.run_id,expected_revision:goal.revision};const key=JSON.stringify(captured);const client=requests.current.get(key)||crypto.randomUUID();requests.current.set(key,client);setBusy(true);setError('');try{const result=await api<{goal:Goal}>('goal_replan',{...captured,client_request_id:client});requests.current.delete(key);if(current.current.id===captured.id&&current.current.run_id===captured.run_id)onRequested(result.goal);window.dispatchEvent(new CustomEvent('forge:goal-guidance',{detail:{run_id:captured.run_id,planner_assignment:result.goal.planner_assignment}}));notify('New guidance requested. Resume to run the planner.');}catch(e){if(current.current.id===captured.id&&current.current.run_id===captured.run_id)setError(errorText(e));else notify(errorText(e));}finally{setBusy(false);}}
  return <div className="goal-guidance-actions"><button className="text-button" disabled={busy||conflict} onClick={()=>void request()}>Request new guidance</button>{error && <ErrorNotice error={error}/>}</div>;
}
