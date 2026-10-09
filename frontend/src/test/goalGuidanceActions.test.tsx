import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { GoalGuidanceActions } from '../goalGuidanceActions';
import { GuidanceCard } from '../taskStatus';
import type { Goal } from '../types';
const goal:Goal={id:'goal',run_id:'run',revision:7,status:'paused',execution_mode:'guided',tasks:[{id:'task',text:'Preserve the accepted task',status:'pending'}]};
describe('Explicit goal guidance requests',()=>{
  it('journals planning intent only after a click without automatically resuming work',async()=>{
    const updated={...goal,planner_assignment:{state:'requested' as const}},call=vi.fn(async(_action:string,_data:Record<string,unknown>)=>({goal:updated})),notify=vi.fn(),onRequested=vi.fn();window.pywebview={api:{call}};render(<GoalGuidanceActions goal={goal} conflict={false} notify={notify} onRequested={onRequested}/>);expect(call).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole('button',{name:'Request new guidance'}));await waitFor(()=>expect(onRequested).toHaveBeenCalledWith(updated));expect(call).toHaveBeenCalledExactlyOnceWith('goal_replan',{id:'goal',run_id:'run',expected_revision:7,client_request_id:expect.any(String)});expect(notify).toHaveBeenCalledWith('New guidance requested. Resume to run the planner.');
    render(<GuidanceCard assignment={updated.planner_assignment}/>);expect(screen.getByText('OpenRouter: New guidance requested · Resume to run the planner')).toBeTruthy();
  });
  it('preserves the request identity across uncertain responses and keeps errors visible',async()=>{
    const call=vi.fn(async(_action:string,_data:Record<string,unknown>)=>({error:'Inspect outcome-unknown actions before requesting another plan.'}));window.pywebview={api:{call}};render(<GoalGuidanceActions goal={goal} conflict={false} notify={vi.fn()} onRequested={vi.fn()}/>);
    await userEvent.click(screen.getByRole('button',{name:'Request new guidance'}));await screen.findByRole('alert');await userEvent.click(screen.getByRole('button',{name:'Request new guidance'}));await waitFor(()=>expect(call).toHaveBeenCalledTimes(2));const sent=call.mock.calls.map(([,data])=>data!);expect(sent[0]!.client_request_id).toBe(sent[1]!.client_request_id);expect(sent[1]!.expected_revision).toBe(7);
  });
  it('captures the clicked revision while metadata changes and excludes running/conflicted goals',async()=>{
    let done!:(value:unknown)=>void;const pending=new Promise(resolve=>{done=resolve;});const call=vi.fn(async(_action:string,_data:Record<string,unknown>)=>pending);window.pywebview={api:{call}};const props={conflict:false,notify:vi.fn(),onRequested:vi.fn()};const view=render(<GoalGuidanceActions goal={goal} {...props}/>);fireEvent.click(screen.getByRole('button',{name:'Request new guidance'}));view.rerender(<GoalGuidanceActions goal={{...goal,revision:8}} {...props}/>);expect(call).toHaveBeenCalledWith('goal_replan',expect.objectContaining({expected_revision:7}));done({goal});await waitFor(()=>expect(props.onRequested).toHaveBeenCalled());view.rerender(<GoalGuidanceActions goal={{...goal,status:'running'}} {...props}/>);expect(screen.queryByRole('button',{name:'Request new guidance'})).toBeNull();view.rerender(<GoalGuidanceActions goal={goal} {...props} conflict/>);expect(screen.getByRole('button',{name:'Request new guidance'})).toHaveProperty('disabled',true);
  });
  it('does not redirect a newly selected goal after a delayed guidance request',async()=>{
    let done!:(value:unknown)=>void;const pending=new Promise(resolve=>{done=resolve;});window.pywebview={api:{call:vi.fn(async()=>pending)}};const props={conflict:false,notify:vi.fn(),onRequested:vi.fn()};const view=render(<GoalGuidanceActions goal={goal} {...props}/>);fireEvent.click(screen.getByRole('button',{name:'Request new guidance'}));view.rerender(<GoalGuidanceActions goal={{...goal,id:'other',run_id:'other-run'}} {...props}/>);done({goal});await waitFor(()=>expect(props.notify).toHaveBeenCalledWith('New guidance requested. Resume to run the planner.'));expect(props.onRequested).not.toHaveBeenCalled();
  });
});
