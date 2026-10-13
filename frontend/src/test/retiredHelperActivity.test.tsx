import { render,screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { expect,it,vi } from 'vitest';
import { ChatActivity } from '../chatActivity';
it('keeps retired helper findings expandable without offering a deleted conversation',async()=>{
  const call=vi.fn(async()=>({run_id:'helper',events:[],children:[],next_cursor:0,has_more:false,status:'completed'}));window.pywebview={api:{call}};
  render(<ChatActivity helper group={{run_id:'helper',status:'completed',current_step:'Research completed',tool_count:2,helper_count:0,children:[],summary:'Synthetic retained findings.',conversation_removed:true,summary_truncated:true}}/>);
  expect(screen.queryByText('Synthetic retained findings.')).toBeNull();expect(call).not.toHaveBeenCalled();await userEvent.click(screen.getByRole('button',{name:/Research completed/}));expect(await screen.findByText('Synthetic retained findings.')).toBeTruthy();expect(screen.getByText(/The retained summary is shortened/)).toBeTruthy();expect(screen.queryByRole('button',{name:/Open chat/})).toBeNull();expect(call).toHaveBeenCalledWith('run_activity',{run_id:'helper',after:0,limit:100});
});
