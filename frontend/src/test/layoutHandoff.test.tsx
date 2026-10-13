import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { DEFAULT_LAYOUT, useWorkspaceLayout } from '../workspaceLayout';
import { updateReadiness } from '../updateReadiness';
const deferred=<T,>()=>{let resolve!:(value:T)=>void;const promise=new Promise<T>(done=>{resolve=done;});return {promise,resolve};};
const attempt='a'.repeat(32);
let controller:ReturnType<typeof useWorkspaceLayout>;
function Harness({notify=()=>{}}:{notify?:(message:string)=>void}){
  controller=useWorkspaceLayout(true,notify);
  return <><span>{controller.layout.sidebar_width}</span><span data-testid="layout-error">{controller.error || 'none'}</span><span data-testid="layout-ready">{String(controller.ready)}</span><span data-testid="layout-retrying">{String(controller.retrying)}</span><button onClick={()=>controller.update({sidebar_width:300})}>First width</button><button onClick={()=>controller.update({sidebar_width:340})}>Second width</button><button onClick={()=>controller.update({sidebar_width:260})}>Default width</button></>;
}
async function confirmation(){
  let result;
  await act(async()=>{expect(updateReadiness.start(attempt,vi.fn())).toBe(true);result=await updateReadiness.confirm(attempt);updateReadiness.aborted(attempt);});
  return result;
}
afterEach(()=>{updateReadiness.aborted(attempt);updateReadiness.dismiss();delete window.pywebview;vi.unstubAllGlobals();});
describe('layout ownership during native readiness',()=>{
  it('never sends an older queued browser layout to a newer desktop identity',async()=>{
    delete window.pywebview;const saved=deferred<Response>();let writes=0;const browser=vi.fn(async(_url:unknown,request:RequestInit)=>{const action=String(_url).split('/').pop(),data=JSON.parse(String(request.body));if(action==='ui_layout_get')return {ok:true,json:async()=>DEFAULT_LAYOUT};writes++;if(writes===1)return saved.promise;return {ok:true,json:async()=>data.layout};});vi.stubGlobal('fetch',browser);
    render(<Harness/>);await screen.findByText('260');await act(async()=>{});fireEvent.click(screen.getByRole('button',{name:'First width'}));await waitFor(()=>expect(writes).toBe(1));fireEvent.click(screen.getByRole('button',{name:'Second width'}));await act(async()=>{await new Promise(resolve=>setTimeout(resolve,350));});
    const desktop=vi.fn(async(action:string)=>action==='ui_layout_get'?{...DEFAULT_LAYOUT,sidebar_width:280}:{});window.pywebview={api:{call:desktop}};await act(async()=>window.dispatchEvent(new Event('pywebviewready')));await screen.findByText('280');
    await act(async()=>saved.resolve({ok:true,json:async()=>({...DEFAULT_LAYOUT,sidebar_width:300})} as Response));await act(async()=>{await new Promise(resolve=>setTimeout(resolve,350));});
    expect(desktop.mock.calls.some(([action])=>action==='ui_layout_save')).toBe(false);expect(screen.getByText('280')).toBeTruthy();
  });
  it('does not call a failed load a confirmed layout',async()=>{
    const call=vi.fn(async()=>({error:'synthetic unavailable'}));window.pywebview={api:{call}};const notify=vi.fn();function Failed(){const layout=useWorkspaceLayout(true,notify);return <span>{layout.ready?'confirmed':'loading'}</span>;}
    render(<Failed/>);await waitFor(()=>expect(notify).toHaveBeenCalled());expect(screen.getByText('loading')).toBeTruthy();expect(screen.queryByText('confirmed')).toBeNull();
  });
  it('recovers a transient load only after a real read acknowledgement',async()=>{
    let reads=0;
    const call=vi.fn(async(action:string)=>action==='ui_layout_get' && ++reads===1 ? {error:'Synthetic unavailable'} : {...DEFAULT_LAYOUT,sidebar_width:310});
    window.pywebview={api:{call}};render(<Harness/>);
    await waitFor(()=>expect(controller.error).toBe('load'));
    expect(controller.ready).toBe(false);expect(await confirmation()).toEqual({ok:false,reason:'preferences_pending'});
    let recovered=false;await act(async()=>{recovered=await controller.retry();});
    expect(recovered).toBe(true);expect(controller.ready).toBe(true);expect(controller.error).toBeNull();expect(controller.retrying).toBe(false);
    expect(screen.getByText('310')).toBeTruthy();expect(call.mock.calls).toEqual([['ui_layout_get',{client_id:'desktop'}],['ui_layout_get',{client_id:'desktop'}]]);
    expect(await confirmation()).toMatchObject({ok:true});
  });
  it('preserves edits during load recovery and confirms their latest value',async()=>{
    let reads=0;const reading=deferred<unknown>(),writing=deferred<unknown>();
    const call=vi.fn(async(action:string)=>action==='ui_layout_get' ? (++reads===1 ? {error:'Synthetic unavailable'} : reading.promise) : writing.promise);
    window.pywebview={api:{call}};render(<Harness/>);await waitFor(()=>expect(controller.error).toBe('load'));
    fireEvent.click(screen.getByRole('button',{name:'First width'}));let result!:Promise<boolean>;
    act(()=>{result=controller.retry();});expect(controller.retrying).toBe(true);
    fireEvent.click(screen.getByRole('button',{name:'Second width'}));await act(async()=>reading.resolve({...DEFAULT_LAYOUT,sidebar_width:280}));
    await waitFor(()=>expect(call).toHaveBeenCalledWith('ui_layout_save',{client_id:'desktop',layout:{...DEFAULT_LAYOUT,sidebar_width:340}}));
    expect(screen.getByText('340')).toBeTruthy();expect(controller.retrying).toBe(true);expect(await confirmation()).toEqual({ok:false,reason:'preferences_pending'});
    let recovered=false;await act(async()=>{writing.resolve({...DEFAULT_LAYOUT,sidebar_width:340});recovered=await result;});
    expect(recovered).toBe(true);expect(controller.error).toBeNull();expect(await confirmation()).toMatchObject({ok:true});
  });
  it('keeps a failed retry blocked instead of confirming fallback defaults',async()=>{
    const call=vi.fn(async(_action:string)=>({error:'Synthetic unavailable'}));window.pywebview={api:{call}};render(<Harness/>);
    await waitFor(()=>expect(controller.error).toBe('load'));let recovered=true;await act(async()=>{recovered=await controller.retry();});
    expect(recovered).toBe(false);expect(controller.ready).toBe(false);expect(controller.error).toBe('load');expect(controller.retrying).toBe(false);
    expect(await confirmation()).toEqual({ok:false,reason:'preferences_pending'});expect(call.mock.calls.every(([action])=>action==='ui_layout_get')).toBe(true);
  });
  it('ignores an old browser save failure after a successful desktop restore',async()=>{
    delete window.pywebview;const saving=deferred<Response>(),notify=vi.fn();
    vi.stubGlobal('fetch',vi.fn(async(url:string)=>url.endsWith('ui_layout_get') ? {ok:true,json:async()=>DEFAULT_LAYOUT} : saving.promise));
    render(<Harness notify={notify}/>);await waitFor(()=>expect(controller.ready).toBe(true));fireEvent.click(screen.getByRole('button',{name:'First width'}));
    await waitFor(()=>expect(fetch).toHaveBeenCalledTimes(2));
    const desktop=vi.fn(async(_action:string)=>({...DEFAULT_LAYOUT,sidebar_width:280}));window.pywebview={api:{call:desktop}};
    await act(async()=>window.dispatchEvent(new Event('pywebviewready')));await screen.findByText('280');
    await act(async()=>saving.resolve({ok:false,status:503,json:async()=>({error:'Synthetic old write'})} as Response));
    expect(controller.error).toBeNull();expect(controller.ready).toBe(true);expect(notify).not.toHaveBeenCalled();expect(await confirmation()).toMatchObject({ok:true});
    expect(desktop.mock.calls.some(([action])=>action==='ui_layout_save')).toBe(false);
  });
  it('can save and recover the desktop layout while an older browser write remains in flight',async()=>{
    delete window.pywebview;const saving=deferred<Response>();
    vi.stubGlobal('fetch',vi.fn(async(url:string)=>url.endsWith('ui_layout_get') ? {ok:true,json:async()=>DEFAULT_LAYOUT} : saving.promise));
    render(<Harness/>);await waitFor(()=>expect(controller.ready).toBe(true));fireEvent.click(screen.getByRole('button',{name:'First width'}));await waitFor(()=>expect(fetch).toHaveBeenCalledTimes(2));
    let writes=0;const desktop=vi.fn(async(action:string,data:Record<string,unknown>)=>action==='ui_layout_get'?{...DEFAULT_LAYOUT,sidebar_width:280}:++writes===1?{error:'Synthetic desktop unavailable'}:data.layout);
    window.pywebview={api:{call:desktop}};await act(async()=>window.dispatchEvent(new Event('pywebviewready')));await screen.findByText('280');fireEvent.click(screen.getByRole('button',{name:'Second width'}));await waitFor(()=>expect(controller.error).toBe('save'));
    let recovered=false;await act(async()=>{recovered=await controller.retry();});expect(recovered).toBe(true);expect(writes).toBe(2);expect(await confirmation()).toMatchObject({ok:true});
    expect(desktop).toHaveBeenLastCalledWith('ui_layout_save',{client_id:'desktop',layout:{...DEFAULT_LAYOUT,sidebar_width:340}});
    await act(async()=>saving.resolve({ok:false,status:503,json:async()=>({error:'Synthetic old write'})} as Response));expect(controller.error).toBeNull();expect(screen.getByText('340')).toBeTruthy();
  });
  it.each(['success','failure'])('ignores an old browser load %s after a desktop restore',async outcome=>{
    delete window.pywebview;const reading=deferred<Response>(),notify=vi.fn();vi.stubGlobal('fetch',vi.fn(()=>reading.promise));
    render(<Harness notify={notify}/>);const desktop=vi.fn(async()=>({...DEFAULT_LAYOUT,sidebar_width:280}));window.pywebview={api:{call:desktop}};
    await act(async()=>window.dispatchEvent(new Event('pywebviewready')));await screen.findByText('280');
    await act(async()=>reading.resolve({ok:outcome==='success',status:503,json:async()=>outcome==='success'?{...DEFAULT_LAYOUT,sidebar_width:350}:{error:'Synthetic old read'}} as Response));
    expect(controller.ready).toBe(true);expect(controller.error).toBeNull();expect(screen.getByText('280')).toBeTruthy();expect(notify).not.toHaveBeenCalled();
  });
  it.each(['success','failure'])('does not let a stale browser retry %s acknowledge the desktop identity',async outcome=>{
    delete window.pywebview;let reads=0;const reading=deferred<Response>(),notify=vi.fn();
    vi.stubGlobal('fetch',vi.fn(async()=>++reads===1?{ok:false,status:503,json:async()=>({error:'Synthetic unavailable'})}:reading.promise));
    render(<Harness notify={notify}/>);await waitFor(()=>expect(controller.error).toBe('load'));let result!:Promise<boolean>;
    act(()=>{result=controller.retry();});expect(controller.retrying).toBe(true);
    const desktop=vi.fn(async()=>({...DEFAULT_LAYOUT,sidebar_width:280}));window.pywebview={api:{call:desktop}};
    await act(async()=>window.dispatchEvent(new Event('pywebviewready')));await screen.findByText('280');
    let recovered=true;await act(async()=>{reading.resolve({ok:outcome==='success',status:503,json:async()=>outcome==='success'?{...DEFAULT_LAYOUT,sidebar_width:350}:{error:'Synthetic old retry'}} as Response);recovered=await result;});
    expect(recovered).toBe(false);expect(controller.error).toBeNull();expect(controller.retrying).toBe(false);expect(controller.ready).toBe(true);expect(screen.getByText('280')).toBeTruthy();expect(notify).toHaveBeenCalledOnce();
    expect(desktop.mock.calls).toEqual([['ui_layout_get',{client_id:'desktop'}]]);expect(await confirmation()).toMatchObject({ok:true});
  });
  it('forces an acknowledged retry when a failed write is followed by the original saved value',async()=>{
    const writing=deferred<unknown>();let writes=0;
    const call=vi.fn(async(action:string,data:Record<string,unknown>)=>action==='ui_layout_get'?DEFAULT_LAYOUT:++writes===1?writing.promise:data.layout);
    window.pywebview={api:{call}};render(<Harness/>);await waitFor(()=>expect(controller.ready).toBe(true));fireEvent.click(screen.getByRole('button',{name:'First width'}));await waitFor(()=>expect(writes).toBe(1));
    fireEvent.click(screen.getByRole('button',{name:'Default width'}));await act(async()=>writing.resolve({error:'Synthetic write unavailable'}));
    expect(controller.error).toBe('save');expect(await confirmation()).toEqual({ok:false,reason:'preferences_pending'});
    let recovered=false;await act(async()=>{recovered=await controller.retry();});
    expect(recovered).toBe(true);expect(writes).toBe(2);expect(call).toHaveBeenLastCalledWith('ui_layout_save',{client_id:'desktop',layout:DEFAULT_LAYOUT});expect(controller.error).toBeNull();expect(await confirmation()).toMatchObject({ok:true});
  });
  it('serializes writes to the same identity and waits for the latest queued acknowledgement',async()=>{
    const first=deferred<unknown>(),second=deferred<unknown>();let writes=0;
    const call=vi.fn(async(action:string)=>action==='ui_layout_get'?DEFAULT_LAYOUT:++writes===1?first.promise:second.promise);
    window.pywebview={api:{call}};render(<Harness/>);await waitFor(()=>expect(controller.ready).toBe(true));fireEvent.click(screen.getByRole('button',{name:'First width'}));await waitFor(()=>expect(writes).toBe(1));
    fireEvent.click(screen.getByRole('button',{name:'Second width'}));await act(async()=>{await new Promise(resolve=>setTimeout(resolve,350));});expect(writes).toBe(1);
    let result!:ReturnType<typeof updateReadiness.confirm>;act(()=>{expect(updateReadiness.start(attempt,vi.fn())).toBe(true);result=updateReadiness.confirm(attempt);});
    await act(async()=>first.resolve({...DEFAULT_LAYOUT,sidebar_width:300}));await waitFor(()=>expect(writes).toBe(2));expect(screen.getByText('340')).toBeTruthy();expect(updateReadiness.getSnapshot().phase).toBe('checking');
    await act(async()=>second.resolve({...DEFAULT_LAYOUT,sidebar_width:340}));let confirmed;await act(async()=>{confirmed=await result;updateReadiness.aborted(attempt);});
    expect(confirmed).toMatchObject({ok:true});expect(writes).toBe(2);expect(controller.error).toBeNull();expect(call).toHaveBeenLastCalledWith('ui_layout_save',{client_id:'desktop',layout:{...DEFAULT_LAYOUT,sidebar_width:340}});
  });
  it('keeps a failed save retry blocked after its actual write is refused',async()=>{
    const call=vi.fn(async(action:string)=>action==='ui_layout_get'?DEFAULT_LAYOUT:{error:'Synthetic write unavailable'});
    window.pywebview={api:{call}};render(<Harness/>);await waitFor(()=>expect(controller.ready).toBe(true));fireEvent.click(screen.getByRole('button',{name:'First width'}));await waitFor(()=>expect(controller.error).toBe('save'));
    let recovered=true;await act(async()=>{recovered=await controller.retry();});expect(recovered).toBe(false);expect(controller.error).toBe('save');expect(controller.retrying).toBe(false);
    expect(call.mock.calls.filter(([action])=>action==='ui_layout_save')).toHaveLength(2);expect(await confirmation()).toEqual({ok:false,reason:'preferences_pending'});
  });
  it.each(['success','failure'])('ignores a retired browser save retry %s after native restoration',async outcome=>{
    delete window.pywebview;const saving=deferred<Response>();let writes=0;
    vi.stubGlobal('fetch',vi.fn(async(url:string)=>url.endsWith('ui_layout_get')?{ok:true,json:async()=>DEFAULT_LAYOUT}:++writes===1?{ok:false,status:503,json:async()=>({error:'Synthetic unavailable'})}:saving.promise));
    render(<Harness/>);await waitFor(()=>expect(controller.ready).toBe(true));fireEvent.click(screen.getByRole('button',{name:'First width'}));await waitFor(()=>expect(controller.error).toBe('save'));let result!:Promise<boolean>;
    act(()=>{result=controller.retry();});await waitFor(()=>expect(writes).toBe(2));const desktop=vi.fn(async()=>({...DEFAULT_LAYOUT,sidebar_width:280}));window.pywebview={api:{call:desktop}};
    await act(async()=>window.dispatchEvent(new Event('pywebviewready')));await screen.findByText('280');let recovered=true;
    await act(async()=>{saving.resolve({ok:outcome==='success',status:503,json:async()=>outcome==='success'?{...DEFAULT_LAYOUT,sidebar_width:300}:{error:'Synthetic old write'}} as Response);recovered=await result;});
    expect(recovered).toBe(false);expect(controller.error).toBeNull();expect(controller.retrying).toBe(false);expect(screen.getByText('280')).toBeTruthy();expect(await confirmation()).toMatchObject({ok:true});expect(desktop).toHaveBeenCalledOnce();
  });
  it('keeps edits made during a save retry and requires their own acknowledgement',async()=>{
    const writing=deferred<unknown>();let writes=0;
    const call=vi.fn(async(action:string,data:Record<string,unknown>)=>action==='ui_layout_get'?DEFAULT_LAYOUT:++writes===1?{error:'Synthetic write unavailable'}:writes===2?writing.promise:data.layout);
    window.pywebview={api:{call}};render(<Harness/>);await waitFor(()=>expect(controller.ready).toBe(true));fireEvent.click(screen.getByRole('button',{name:'First width'}));await waitFor(()=>expect(controller.error).toBe('save'));
    let result!:Promise<boolean>;act(()=>{result=controller.retry();});await waitFor(()=>expect(writes).toBe(2));fireEvent.click(screen.getByRole('button',{name:'Second width'}));
    let recovered=true;await act(async()=>{writing.resolve({...DEFAULT_LAYOUT,sidebar_width:300});recovered=await result;});
    expect(recovered).toBe(false);expect(screen.getByText('340')).toBeTruthy();expect(controller.error).toBe('save');expect(await confirmation()).toEqual({ok:false,reason:'preferences_pending'});
    await act(async()=>{recovered=await controller.retry();});expect(recovered).toBe(true);expect(writes).toBe(3);expect(call).toHaveBeenLastCalledWith('ui_layout_save',{client_id:'desktop',layout:{...DEFAULT_LAYOUT,sidebar_width:340}});expect(await confirmation()).toMatchObject({ok:true});
  });
  it.each([{}, {...DEFAULT_LAYOUT,sidebar_width:280}])('rejects an incomplete or unrelated write acknowledgement %#',async invalid=>{
    let reject=true;const call=vi.fn(async(action:string,data:Record<string,unknown>)=>action==='ui_layout_get'?DEFAULT_LAYOUT:reject?invalid:data.layout);
    window.pywebview={api:{call}};render(<Harness/>);await waitFor(()=>expect(controller.ready).toBe(true));fireEvent.click(screen.getByRole('button',{name:'First width'}));await waitFor(()=>expect(controller.error).toBe('save'));
    expect(screen.getByText('300')).toBeTruthy();expect(await confirmation()).toEqual({ok:false,reason:'preferences_pending'});
    reject=false;let recovered=false;await act(async()=>{recovered=await controller.retry();});expect(recovered).toBe(true);expect(controller.error).toBeNull();expect(await confirmation()).toMatchObject({ok:true});
  });
  it('does not confirm a missing load payload by substituting default preferences',async()=>{
    const call=vi.fn(async()=>({}));window.pywebview={api:{call}};render(<Harness/>);await waitFor(()=>expect(controller.error).toBe('load'));
    expect(controller.ready).toBe(false);expect(await confirmation()).toEqual({ok:false,reason:'preferences_pending'});let recovered=true;await act(async()=>{recovered=await controller.retry();});expect(recovered).toBe(false);expect(controller.error).toBe('load');
  });
  it('adopts the coordinator removal of a deleted collapsed project without another write',async()=>{
    const call=vi.fn(async(action:string,data:Record<string,unknown>)=>action==='ui_layout_get'?{...DEFAULT_LAYOUT,collapsed_project_ids:['deleted-project']}:{...(data.layout as object),collapsed_project_ids:[]});
    window.pywebview={api:{call}};render(<Harness/>);await waitFor(()=>expect(controller.ready).toBe(true));fireEvent.click(screen.getByRole('button',{name:'First width'}));await waitFor(()=>expect(controller.layout.collapsed_project_ids).toEqual([]));
    expect(controller.error).toBeNull();expect(screen.getByText('300')).toBeTruthy();expect(await confirmation()).toMatchObject({ok:true});expect(call.mock.calls.filter(([action])=>action==='ui_layout_save')).toHaveLength(1);
  });
  it('cannot perform a recovery read while restart cancellation remains unconfirmed',async()=>{
    const call=vi.fn(async()=>({error:'Synthetic unavailable'}));window.pywebview={api:{call}};render(<Harness/>);await waitFor(()=>expect(controller.error).toBe('load'));
    act(()=>{expect(updateReadiness.start(attempt,vi.fn())).toBe(true);updateReadiness.aborting('preferences_pending');});
    expect(await controller.retry()).toBe(false);expect(call).toHaveBeenCalledOnce();expect(controller.error).toBe('load');expect(controller.retrying).toBe(false);
    act(()=>{updateReadiness.aborted(attempt,'preferences_pending');});expect(updateReadiness.canEdit()).toBe(true);
    let recovered=true;await act(async()=>{recovered=await controller.retry();});expect(recovered).toBe(false);expect(call).toHaveBeenCalledTimes(2);expect(await confirmation()).toEqual({ok:false,reason:'preferences_pending'});
  });
});
