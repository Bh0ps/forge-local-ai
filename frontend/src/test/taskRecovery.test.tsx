import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { TaskStatus } from '../taskStatus';
import { api } from '../api';
import type { Run } from '../types';

const limit = 'A continuity checkpoint cannot fit this context. Increase Context or disable tools, then Resume. Original records remain saved.';
const paused: Run = {id:'run-a', status:'paused', recovery:limit, progress_summary:{waiting_reason:limit, available_actions:['resume']}};
const unknown = {id:'action-a', name:'write_file', arguments:{path:'page.txt'}, status:'outcome_unknown'};
function deferred<T>() {let resolve!:(value:T)=>void; const promise=new Promise<T>(done=>{resolve=done;}); return {promise, resolve};}
const button = () => screen.getByRole('button', {name:'Resume'}) as HTMLButtonElement;

describe('Inline task recovery', () => {
  it('keeps a context-limit error visible and permits Resume after changing context with no unknown actions', async () => {
    const call=vi.fn(async (action:string) => action==='unknown_actions'?{actions:[]}:action==='settings'?{context:16384}:{id:'run-a', status:'queued'});
    window.pywebview={api:{call}};
    const resume=vi.fn(()=>void api('resume',{id:'run-a'}));
    render(<TaskStatus run={paused} resume={resume} pause={()=>{}} notify={()=>{}}/>);
    await waitFor(()=>expect(button().disabled).toBe(false));
    expect(screen.getAllByText(limit).length).toBeGreaterThan(0);
    await act(async()=>{await api('settings',{context:16384});});
    fireEvent.click(button());
    await waitFor(()=>expect(call).toHaveBeenCalledWith('resume',{id:'run-a'}));
    expect(call).toHaveBeenCalledWith('settings',{context:16384});
    expect(resume).toHaveBeenCalledTimes(1);
  });

  it('blocks unknown actions until an explicit evidenced inspection and a fresh empty coordinator result', async () => {
    let actions=[unknown]; const notify=vi.fn(), resume=vi.fn();
    const refresh=deferred<{actions:typeof actions}>(); let reads=0;
    const call=vi.fn(async (action:string) => {
      if(action==='unknown_actions') return ++reads===1?{actions}:refresh.promise;
      if(action==='resolve_action') {actions=[];return {ok:true};}
      throw new Error(action);
    });
    window.pywebview={api:{call}};
    render(<TaskStatus run={{...paused,progress_summary:{available_actions:['inspect_interruption']}}} resume={resume} pause={()=>{}} notify={notify}/>);
    fireEvent.click(await screen.findByRole('button',{name:'Inspect write_file'}));
    expect(button().disabled).toBe(true);
    expect((screen.getByRole('button',{name:'Verified not executed'}) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(screen.getByRole('textbox',{name:'Inspection evidence'}),{target:{value:'Inspected page.txt; no change was made.'}});
    fireEvent.click(screen.getByRole('button',{name:'Verified not executed'}));
    await waitFor(()=>expect(call).toHaveBeenCalledWith('resolve_action',{invocation_id:'action-a',outcome:'not_executed',evidence:'Inspected page.txt; no change was made.'}));
    expect(button().disabled).toBe(true);
    await act(async()=>refresh.resolve({actions:[]}));
    await waitFor(()=>expect(button().disabled).toBe(false));
    fireEvent.click(button()); expect(resume).toHaveBeenCalledTimes(1);
    expect(notify).toHaveBeenCalledWith('Inspection recorded. Resume when you are ready.');
  });

  it('does not let a late empty inspection enable a different run', async () => {
    const first=deferred<{actions:typeof unknown[]}>();
    const call=vi.fn(async (_action:string,data:{run_id?:string})=>data.run_id==='run-a'?first.promise:{actions:[{...unknown,id:'action-b'}]});
    window.pywebview={api:{call}};
    const props={resume:vi.fn(),pause:()=>{},notify:()=>{}};
    const view=render(<TaskStatus run={paused} {...props}/>);
    view.rerender(<TaskStatus run={{...paused,id:'run-b'}} {...props}/>);
    await screen.findByRole('button',{name:'Inspect write_file'});
    await act(async()=>first.resolve({actions:[]}));
    expect(button().disabled).toBe(true); expect(props.resume).not.toHaveBeenCalled();
  });

  it('does not refresh or notify a different run when an old action resolution returns late', async () => {
    const resolution=deferred<{ok:boolean}>(), notify=vi.fn();
    const call=vi.fn(async (action:string,data:Record<string,unknown>)=>action==='resolve_action'?resolution.promise:{actions:[{...unknown,id:data.run_id==='run-a'?'action-a':'action-b'}]});
    window.pywebview={api:{call}};
    const props={resume:vi.fn(),pause:()=>{},notify};
    const view=render(<TaskStatus run={paused} {...props}/>);
    fireEvent.click(await screen.findByRole('button',{name:'Inspect write_file'}));
    fireEvent.change(screen.getByRole('textbox',{name:'Inspection evidence'}),{target:{value:'Original action inspected.'}});
    fireEvent.click(screen.getByRole('button',{name:'Verified completed'}));
    await waitFor(()=>expect(call).toHaveBeenCalledWith('resolve_action',expect.objectContaining({invocation_id:'action-a'})));
    view.rerender(<TaskStatus run={{...paused,id:'run-b'}} {...props}/>);
    await screen.findByRole('button',{name:'Inspect write_file'});
    await act(async()=>resolution.resolve({ok:true}));
    expect(button().disabled).toBe(true); expect(notify).not.toHaveBeenCalled();
    expect(call.mock.calls.filter(([action,data])=>action==='unknown_actions'&&data.run_id==='run-a')).toHaveLength(1);
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('keeps Resume blocked on an unavailable inspection and retries visibly', async () => {
    let failed=true;window.pywebview={api:{call:vi.fn(async()=>failed?{error:'Coordinator unavailable'}:{actions:[]})}};
    render(<TaskStatus run={paused} resume={()=>{}} pause={()=>{}} notify={()=>{}}/>);
    await screen.findByText('Coordinator unavailable'); expect(button().disabled).toBe(true);
    failed=false;fireEvent.click(screen.getByRole('button',{name:'Retry'}));
    await waitFor(()=>expect(button().disabled).toBe(false));
  });

  it('rechecks the same run after an active attempt pauses again', async () => {
    const next=deferred<{actions:typeof unknown[]}>(); let reads=0;
    window.pywebview={api:{call:vi.fn(async()=>++reads===1?{actions:[]}:next.promise)}};
    const props={resume:vi.fn(),pause:()=>{},notify:()=>{}};
    const view=render(<TaskStatus run={paused} {...props}/>);
    await waitFor(()=>expect(button().disabled).toBe(false));
    view.rerender(<TaskStatus run={{...paused,status:'running',recovery:undefined}} {...props}/>);
    view.rerender(<TaskStatus run={paused} {...props}/>);
    expect(button().disabled).toBe(true);
    await act(async()=>next.resolve({actions:[unknown]}));
    await screen.findByRole('button',{name:'Inspect write_file'}); expect(button().disabled).toBe(true);
  });
});
