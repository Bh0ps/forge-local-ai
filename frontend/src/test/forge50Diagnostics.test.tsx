import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { ActivityCard, RunInspector } from '../runInspector';
import { applyEvents } from '../App';
import { CalibrationSettings } from '../calibrationSettings';
import { DEFAULT_SETTINGS } from '../types';

describe('Forge 5 diagnostics',()=>{
  it('retains failed and passing checks through activity replay',()=>{
    const initial={run:{id:'r'},text:'',thinking:'',parts:[],tools:[],cursor:0,status:'running',finished:false};
    const events=[{seq:1,type:'verification',name:'quality_check',passed:false,failed_count:2},
      {seq:2,type:'verification',name:'quality_check',passed:true}];
    const result=applyEvents(initial,events,{});expect(result.tools).toHaveLength(2);
    expect(applyEvents(result,events,{}).tools).toHaveLength(2);
    render(<><ActivityCard event={result.tools[0]}/><ActivityCard event={result.tools[1]}/></>);
    expect(screen.getByText('Checks failed')).toBeTruthy();expect(screen.getByText('Checks passed')).toBeTruthy();
  });
  it('shows unresolved check evidence separately from successful invocation delivery',async()=>{
    window.pywebview={api:{call:vi.fn(async()=>({workflow_stage:'repair',invocations:[{id:'i',name:'quality_check',status:'completed'}],
      verification:{scope_count:1,changed_since_check:true,repeated_unchanged:false,failures:[{tool:'quality_check',kind:'checks',failed_count:1,invocation_id:'i',failed:[{name:'Keyboard focus returns',detail:'Observed focus outside the trigger.'}]}]}}))}};
    render(<RunInspector runId="r"/>);await userEvent.click(screen.getByRole('button',{name:'Inspect run'}));
    await screen.findByText('Unresolved checks (1)');expect(screen.getByText('Files have changed. These checks need fresh evidence.')).toBeTruthy();
    expect(screen.getByText('Keyboard focus returns')).toBeTruthy();expect(screen.getByText('quality_check · completed')).toBeTruthy();
  });
  it('retains skill and specialist activity through replay',()=>{
    const initial={run:{id:'r'},text:'',thinking:'',parts:[],tools:[],cursor:0,status:'running',finished:false};
    const events=[{seq:1,type:'skill',name:'Frontend implementation',reason:'Current implementation phase',digest:'12345678abcdef'},
      {seq:2,type:'specialist',specialty:'planner',state:'unavailable',text:'Continuing locally'}];
    const result=applyEvents(initial,events,{});expect(result.tools).toHaveLength(2);
    expect(applyEvents(result,events,{}).tools).toHaveLength(2);
    render(<ActivityCard event={result.tools[0]}/>);expect(screen.getByText('Current implementation phase')).toBeTruthy();
  });
  it('loads persisted timings only when requested and labels missing counters',async()=>{
    const call=vi.fn(async()=>({phase:'tools',tools:['read_file'],context_snapshot:{token_breakdown:{skills:50}},invocations:[],
      usage:{requests:[{id:'u',token_breakdown:{skills:50},phase_timings:{queue_seconds:.25,total_seconds:1}}]}}));
    window.pywebview={api:{call}};render(<RunInspector runId="r"/>);expect(call).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole('button',{name:'Inspect run'}));await screen.findByText('0.25 s');
    expect(call).toHaveBeenCalledWith('run_context_inspect',{run_id:'r'});expect(screen.getAllByText('Unavailable')).toHaveLength(4);
  });
  it('keeps adaptive mode off until explicit selection',async()=>{
    window.pywebview={api:{call:vi.fn(async()=>({calibrations:[]}))}};
    const onChange=vi.fn(async()=>{});render(<CalibrationSettings settings={DEFAULT_SETTINGS} onChange={onChange} notify={()=>{}}/>);
    await waitFor(()=>expect(screen.getByRole('switch',{name:'Adaptive local tuning'}).getAttribute('aria-checked')).toBe('false'));expect(onChange).not.toHaveBeenCalled();
  });
});
