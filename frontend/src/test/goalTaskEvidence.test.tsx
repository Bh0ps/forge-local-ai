import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { GoalTaskEvidence } from '../goalTaskEvidence';
import { activeGoals } from '../pages';
import type { Goal } from '../types';
const goal:Goal={id:'goal',run_id:'run',chat_id:'chat',revision:7,execution_mode:'guided',status:'paused',tasks:[{id:'task',text:'Review the visible layout',status:'completed',evidence:['artifact:screenshot']}],progress_summary:{verified_task_ids:[]}};
describe('Explicit human task review',()=>{
  it('requires an observation and deliberate review confirmation before producing a receipt',async()=>{
    const call=vi.fn(async()=>({goal:{...goal,revision:8}})),accepted=vi.fn();window.pywebview={api:{call}};render(<GoalTaskEvidence goal={goal} conflict={false} onAccepted={accepted}/>);expect(call).not.toHaveBeenCalled();
    await userEvent.click(screen.getByText('Review the visible layout'));await userEvent.click(screen.getByRole('button',{name:'Accept checked task'}));expect(screen.getByRole('button',{name:'Record acceptance'})).toHaveProperty('disabled',true);
    await userEvent.type(screen.getByLabelText('What did you check?'),'Inspected the current mobile layout and keyboard focus.');expect(screen.getByRole('button',{name:'Record acceptance'})).toHaveProperty('disabled',true);
    await userEvent.click(screen.getByRole('checkbox',{name:"I reviewed this task's current result"}));await userEvent.click(screen.getByRole('button',{name:'Record acceptance'}));await waitFor(()=>expect(accepted).toHaveBeenCalledOnce());
    expect(call).toHaveBeenCalledWith('goal_task_accept',{id:'goal',run_id:'run',task_id:'task',expected_revision:7,note:'Inspected the current mobile layout and keyboard focus.',evidence_ids:[]});
    expect(screen.getByText('Human review recorded')).toBeTruthy();expect(screen.getByText('Needs verification')).toBeTruthy();
  });
  it('retains the captured revision and note after a goal changes during human review',async()=>{
    const call=vi.fn(async()=>({error:'Goal changed. Review its current tasks before accepting.'}));window.pywebview={api:{call}};const props={conflict:false,onAccepted:vi.fn()};const view=render(<GoalTaskEvidence goal={goal} {...props}/>);
    await userEvent.click(screen.getByText('Review the visible layout'));await userEvent.click(screen.getByRole('button',{name:'Accept checked task'}));fireEvent.change(screen.getByLabelText('What did you check?'),{target:{value:'A concrete observation'}});fireEvent.click(screen.getByRole('checkbox'));
    view.rerender(<GoalTaskEvidence goal={{...goal,revision:8}} {...props}/>);fireEvent.click(screen.getByRole('button',{name:'Record acceptance'}));await screen.findByRole('alert');expect(screen.getByLabelText('What did you check?')).toHaveProperty('value','A concrete observation');expect(call).toHaveBeenCalledWith('goal_task_accept',expect.objectContaining({expected_revision:7}));expect(props.onAccepted).not.toHaveBeenCalled();
  });
  it('keeps legacy tasks and fresh verified tasks out of the human receipt producer',()=>{
    const props={conflict:false,onAccepted:vi.fn()};const view=render(<GoalTaskEvidence goal={{...goal,execution_mode:'legacy'}} {...props}/>);expect(screen.queryByRole('button',{name:'Accept checked task'})).toBeNull();
    view.rerender(<GoalTaskEvidence goal={{...goal,progress_summary:{verified_task_ids:['task']}}} {...props}/>);expect(screen.queryByRole('button',{name:'Accept checked task'})).toBeNull();expect(screen.getByText('Verified')).toBeTruthy();
  });
  it('retains paused guided goals for unresolved human review while preserving legacy filtering',()=>{
    expect(activeGoals([goal,{...goal,id:'legacy',execution_mode:'legacy'}],[{id:'chat',project_id:null,title:'Chat',model:'fixture'}]).map(item=>item.id)).toEqual(['goal']);
  });
});
