import { act,render,screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { expect,it,vi } from 'vitest';
import { CalibrationSettings } from '../calibrationSettings';
import { DEFAULT_SETTINGS } from '../types';
const profile=(model:string)=>({id:model,model,context:32768,validated:true,median_total_seconds:1,quality_scope:'Synthetic probe scope'});
it('retires delayed model and transport reads and never applies a stale approval',async()=>{
  let oldRead!:(value:unknown)=>void,approval!:(value:unknown)=>void;
  const first=vi.fn(async(action:string)=>action==='performance_calibrations'?new Promise(done=>{oldRead=done;}):{});window.pywebview={api:{call:first}};const change=vi.fn(async()=>{}),notify=vi.fn();const props={settings:{...DEFAULT_SETTINGS,model:'old-model',provider_id:'ollama'},onChange:change,notify};const view=render(<CalibrationSettings {...props}/>);
  const current=vi.fn(async(action:string)=>action==='performance_calibrations'?{calibrations:[profile('new-model')]}:new Promise(done=>{approval=done;}));window.pywebview={api:{call:current}};view.rerender(<CalibrationSettings {...props} settings={{...props.settings,model:'new-model'}}/>);await screen.findByText('new-model');await act(async()=>oldRead({calibrations:[profile('old-model')]}));expect(screen.queryByText('old-model')).toBeNull();
  await userEvent.click(screen.getByRole('button',{name:'Approve profile'}));window.pywebview={api:{call:vi.fn(async()=>({calibrations:[]}))}};view.rerender(<CalibrationSettings {...props} settings={{...props.settings,model:'new-model'}}/>);await act(async()=>approval({settings:{...DEFAULT_SETTINGS,adaptive_profile_ids:['new-model']}}));expect(change).not.toHaveBeenCalled();expect(notify).not.toHaveBeenCalled();expect(screen.queryByText('new-model')).toBeNull();
});
it('keeps the latest refresh when an earlier refresh fails',async()=>{
  let fail!:(error:Error)=>void;let reads=0;const call=vi.fn(async()=>++reads===1?new Promise((_,reject)=>{fail=reject;}):{calibrations:[profile('current-model')]});window.pywebview={api:{call}};render(<CalibrationSettings settings={{...DEFAULT_SETTINGS,model:'current-model'}} onChange={async()=>{}} notify={()=>{}}/>);
  await userEvent.click(screen.getByRole('button',{name:'Refresh profiles'}));await screen.findByText('current-model');await act(async()=>fail(new Error('Private stale error')));expect(screen.queryByText('Private stale error')).toBeNull();
});
