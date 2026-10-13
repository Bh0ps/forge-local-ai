import { StrictMode, useLayoutEffect } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { api } from '../api';
import { DEFAULT_LAYOUT, useWorkspaceLayout } from '../workspaceLayout';
import { updateReadiness, useReadinessParticipant, type ReadinessDiagnostic, type ReadinessKind } from '../updateReadiness';

const deferred=<T,>()=>{let resolve!:(value:T)=>void;const promise=new Promise<T>(done=>{resolve=done;});return {promise,resolve};};
let controller:ReturnType<typeof useWorkspaceLayout>;
let beforeEffects:ReadinessDiagnostic;
function Harness({enabled=true}:{enabled?:boolean}){
  controller=useWorkspaceLayout(enabled,()=>{});
  useLayoutEffect(()=>{beforeEffects=updateReadiness.diagnose();});
  return <button onClick={()=>controller.update({sidebar_width:300})}>Resize fixture</button>;
}
const layoutObservation=()=>updateReadiness.diagnose().participants.find(row=>row.kind==='layout');
afterEach(()=>{delete window.pywebview;vi.unstubAllGlobals();});

describe('actual layout readiness metadata',()=>{
  it('distinguishes disabled and uninitialized owners from an actual pending read',async()=>{
    const reading=deferred<unknown>(),call=vi.fn(()=>reading.promise);window.pywebview={api:{call}};
    const view=render(<StrictMode><Harness enabled={false}/></StrictMode>);
    expect(layoutObservation()).toEqual({kind:'layout',reasons:[],state:'disabled',load_started:false});expect(call).not.toHaveBeenCalled();
    view.rerender(<StrictMode><Harness/></StrictMode>);
    expect(beforeEffects.participants.find(row=>row.kind==='layout')).toEqual({kind:'layout',reasons:['preferences_pending'],state:'uninitialized',load_started:false});
    expect(layoutObservation()).toEqual({kind:'layout',reasons:['preferences_pending'],state:'loading',load_started:true});expect(call).toHaveBeenCalledWith('ui_layout_get',{client_id:'desktop'});
    await act(async()=>reading.resolve(DEFAULT_LAYOUT));expect(layoutObservation()).toEqual({kind:'layout',reasons:[],state:'ready',load_started:true});
    view.rerender(<StrictMode><Harness enabled={false}/></StrictMode>);expect(layoutObservation()).toEqual({kind:'layout',reasons:[],state:'disabled',load_started:true});
    const serialized=JSON.stringify(updateReadiness.diagnose());expect(serialized).not.toMatch(/client_id|collapsed_project_ids|sidebar_width|receipt|content|scope|fingerprint/);
  });
  it('keeps new native load metadata independent of a retired browser read',async()=>{
    delete window.pywebview;const browserRead=deferred<Response>(),nativeRead=deferred<unknown>();vi.stubGlobal('fetch',vi.fn(()=>browserRead.promise));
    render(<Harness/>);expect(layoutObservation()).toEqual({kind:'layout',reasons:['preferences_pending'],state:'loading',load_started:true});
    const call=vi.fn(()=>nativeRead.promise);window.pywebview={api:{call}};await act(async()=>window.dispatchEvent(new Event('pywebviewready')));expect(call).toHaveBeenCalledWith('ui_layout_get',{client_id:'desktop'});
    await act(async()=>browserRead.resolve({ok:false,status:503,json:async()=>({error:'Synthetic private old read'})} as Response));expect(layoutObservation()).toEqual({kind:'layout',reasons:['preferences_pending'],state:'loading',load_started:true});
    await act(async()=>nativeRead.resolve(DEFAULT_LAYOUT));expect(layoutObservation()).toEqual({kind:'layout',reasons:[],state:'ready',load_started:true});
  });
  it('reports a real failed load and retry without confirming pending work',async()=>{
    const reading=deferred<unknown>();let reads=0;
    window.pywebview={api:{call:vi.fn(async()=>++reads===1?{error:'Synthetic private load failure'}:reading.promise)}};
    render(<Harness/>);await waitFor(()=>expect(controller.error).toBe('load'));
    expect(layoutObservation()).toEqual({kind:'layout',reasons:['preferences_pending'],state:'load_failed',load_started:true});let result!:Promise<boolean>;
    act(()=>{result=controller.retry();});expect(layoutObservation()).toEqual({kind:'layout',reasons:['preferences_pending'],state:'retrying',load_started:true});expect(controller.ready).toBe(false);
    await act(async()=>{reading.resolve(DEFAULT_LAYOUT);expect(await result).toBe(true);});expect(layoutObservation()).toEqual({kind:'layout',reasons:[],state:'ready',load_started:true});expect(JSON.stringify(updateReadiness.diagnose())).not.toContain('Synthetic private');
  });
  it('reports save failure until a real current-value retry is acknowledged',async()=>{
    const saving=deferred<unknown>();let writes=0;
    window.pywebview={api:{call:vi.fn(async(action:string)=>action==='ui_layout_get'?DEFAULT_LAYOUT:++writes===1?{error:'Synthetic private save failure'}:saving.promise)}};
    render(<Harness/>);await waitFor(()=>expect(controller.ready).toBe(true));fireEvent.click(screen.getByRole('button',{name:'Resize fixture'}));await waitFor(()=>expect(controller.error).toBe('save'));
    expect(layoutObservation()).toEqual({kind:'layout',reasons:['preferences_pending'],state:'save_failed',load_started:true});let result!:Promise<boolean>;
    act(()=>{result=controller.retry();});expect(layoutObservation()!.state).toBe('retrying');expect(layoutObservation()!.reasons).toEqual(['preferences_pending']);
    await act(async()=>{saving.resolve({...DEFAULT_LAYOUT,sidebar_width:300});expect(await result).toBe(true);});expect(layoutObservation()!.state).toBe('ready');expect(layoutObservation()!.reasons).toEqual([]);
  });
  it('forwards current hook metadata without changing participant membership on rerender',()=>{
    function Owner({kind}:{kind:ReadinessKind}){useReadinessParticipant({kind,inspect:()=>({reasons:[],state:'ready'}),flushAndConfirm:async()=>({ok:true,receipts:[]})});return null;}
    const before=updateReadiness.participantCount(),view=render(<Owner kind="other"/>);expect(updateReadiness.participantCount()).toBe(before+1);expect(updateReadiness.diagnose().participants.at(-1)).toEqual({kind:'other',reasons:[],state:'ready'});
    view.rerender(<Owner kind="workspace"/>);expect(updateReadiness.participantCount()).toBe(before+1);expect(updateReadiness.diagnose().participants.at(-1)).toEqual({kind:'workspace',reasons:[],state:'ready'});
    view.unmount();expect(updateReadiness.participantCount()).toBe(before);
  });
  it('reports bounded request activity without request payloads or changing admission',async()=>{
    const saving=deferred<unknown>();window.pywebview={api:{call:vi.fn(()=>saving.promise)}};
    let result!:Promise<unknown>;act(()=>{result=api('settings',{theme:'dark',model:'synthetic-private-model'});});
    const requests=updateReadiness.diagnose().participants.find(row=>row.kind==='requests');expect(requests).toEqual({kind:'requests',reasons:['submission_pending'],pending:1,state:'waiting'});expect(JSON.stringify(updateReadiness.diagnose())).not.toContain('synthetic-private');
    await act(async()=>{saving.resolve({ok:true});await result;});expect(updateReadiness.diagnose().participants.find(row=>row.kind==='requests')).toEqual({kind:'requests',reasons:[],pending:0,state:'ready'});
  });
});
