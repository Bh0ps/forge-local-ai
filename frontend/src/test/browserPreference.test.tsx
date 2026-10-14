import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { BrowserPanel } from '../browserPanel';
import { DEFAULT_LAYOUT, useWorkspaceLayout } from '../workspaceLayout';
import { updateReadiness } from '../updateReadiness';

const deferred=<T,>()=>{let resolve!:(value:T)=>void,reject!:(error:Error)=>void;const promise=new Promise<T>((yes,no)=>{resolve=yes;reject=no;});return{promise,resolve,reject};};
const desktop={chat_id:'first',scope_run_id:null,mode:'shared_desktop',run_id:null,invocation_id:null,tool:null,status:'idle',action_count:0,action_count_truncated:false,evidence_mode:'none',snapshot:null,failure:null};
let layout:ReturnType<typeof useWorkspaceLayout>;
function Harness(){
  layout=useWorkspaceLayout(true,()=>{});
  return <BrowserPanel chatId="first" browserMode={layout.layout.browser_mode} preferenceReady={layout.ready}
    preferenceError={layout.error} preferenceRetrying={layout.retrying} onPreferenceRetry={layout.retry}
    onModeChange={browser_mode=>layout.update({browser_mode})}/>;
}
function bridge(overrides:Record<string,unknown>={}){
  const replies:Record<string,unknown>={ui_layout_get:DEFAULT_LAYOUT,browser_native_status:{running:true,visible:true},
    browser_native_show:{ok:true},browser_native_hide:{ok:true},computer_view_state:desktop,...overrides};
  const call=vi.fn(async(action:string,data:Record<string,unknown>)=>{
    if(action==='ui_layout_save' && !(action in replies))return data.layout;
    if(!(action in replies))throw new Error('Unexpected synthetic action');
    const reply=replies[action];return typeof reply==='function'?reply(data):reply;
  });window.pywebview={api:{call}};return{call,replies};
}
async function tick(ms=1){await act(async()=>{await vi.advanceTimersByTimeAsync(ms);});}
beforeEach(()=>{
  vi.useFakeTimers();vi.stubGlobal('ResizeObserver',class{observe(){}disconnect(){}});
  vi.stubGlobal('requestAnimationFrame',(callback:FrameRequestCallback)=>window.setTimeout(()=>callback(performance.now()),1));
  vi.stubGlobal('cancelAnimationFrame',(id:number)=>clearTimeout(id));
  vi.spyOn(Element.prototype,'getBoundingClientRect').mockReturnValue({x:900,y:90,width:320,height:600} as DOMRect);
});
afterEach(()=>{cleanup();updateReadiness.aborted('view-fixture');updateReadiness.dismiss();vi.clearAllTimers();vi.useRealTimers();vi.unstubAllGlobals();vi.restoreAllMocks();delete window.pywebview;});
describe('remembered workspace browser view',()=>{
  it('awaits current preference and Hide before restoring Desktop without starting a browser',async()=>{
    const loading=deferred(),hidden=deferred();const{call}=bridge({ui_layout_get:loading.promise,browser_native_hide:hidden.promise});
    render(<Harness/>);await tick(100);
    expect(call.mock.calls.map(([action])=>action)).toEqual(['ui_layout_get']);
    expect((screen.getByRole('button',{name:'Desktop'}) as HTMLButtonElement).disabled).toBe(true);
    await act(async()=>loading.resolve({...DEFAULT_LAYOUT,browser_mode:'desktop'}));
    expect(screen.getByText('Hiding the browser…')).toBeTruthy();
    expect(call.mock.calls.map(([action])=>action)).toEqual(['ui_layout_get','browser_native_hide']);
    await act(async()=>hidden.resolve({ok:true}));
    expect(screen.getByRole('region',{name:'Desktop activity'})).toBeTruthy();
    expect(call.mock.calls.map(([action])=>action)).toEqual(['ui_layout_get','browser_native_hide','computer_view_state']);
  });
  it('preserves an unrelated pre-load resize while adopting the saved Desktop preference',async()=>{
    const loading=deferred(),{call}=bridge({ui_layout_get:loading.promise});render(<Harness/>);await tick();
    act(()=>layout.update({sidebar_width:330}));
    await act(async()=>loading.resolve({...DEFAULT_LAYOUT,browser_mode:'desktop',sidebar_width:280}));await tick(350);
    expect(layout.layout.sidebar_width).toBe(330);expect(layout.layout.browser_mode).toBe('desktop');
    expect(screen.getByRole('region',{name:'Desktop activity'})).toBeTruthy();
    expect(call.mock.calls.some(([action])=>action==='browser_native_status' || action==='browser_native_show')).toBe(false);
    expect(call).toHaveBeenCalledWith('ui_layout_save',{client_id:'desktop',layout:{...DEFAULT_LAYOUT,sidebar_width:330,browser_mode:'desktop'}});
  });
  it('remembers a user choice only through the existing strict layout write',async()=>{
    const{call}=bridge();const view=render(<Harness/>);await tick();fireEvent.click(screen.getByRole('button',{name:'Desktop'}));await tick(350);
    expect(call).toHaveBeenCalledWith('ui_layout_save',{client_id:'desktop',layout:{...DEFAULT_LAYOUT,browser_mode:'desktop'}});
    expect(layout.error).toBeNull();view.unmount();
    const restored=bridge({ui_layout_get:{...DEFAULT_LAYOUT,browser_mode:'desktop'}});render(<Harness/>);await tick();
    expect(screen.getByRole('region',{name:'Desktop activity'})).toBeTruthy();
    expect(restored.call.mock.calls.some(([action])=>action==='browser_native_show' || action==='browser_native_status')).toBe(false);
  });
  it('cannot acknowledge an omitted nondefault mode while allowing legacy default reads',async()=>{
    const legacy={...DEFAULT_LAYOUT};delete(legacy as Partial<typeof legacy>).browser_mode;
    bridge({ui_layout_get:legacy,ui_layout_save:legacy});render(<Harness/>);await tick();
    expect(layout.ready).toBe(true);expect(layout.layout.browser_mode).toBe('browser');
    fireEvent.click(screen.getByRole('button',{name:'Desktop'}));await tick(350);
    expect(layout.error).toBe('save');
    let result;await act(async()=>{expect(updateReadiness.start('view-fixture',vi.fn())).toBe(true);result=await updateReadiness.confirm('view-fixture');});
    expect(result).toEqual({ok:false,reason:'preferences_pending'});
  });
  it('offers a real scoped retry for a failed preference load without starting a browser',async()=>{
    let reads=0;const{call}=bridge({ui_layout_get:()=>++reads===1?{error:'Synthetic unavailable'}:{...DEFAULT_LAYOUT,browser_mode:'desktop'}});
    render(<Harness/>);await tick();expect(screen.getByText('The workspace view preference could not be loaded.')).toBeTruthy();
    expect(call.mock.calls.map(([action])=>action)).toEqual(['ui_layout_get']);
    fireEvent.click(screen.getByRole('button',{name:'Retry view preference'}));await tick();
    expect(screen.getByRole('region',{name:'Desktop activity'})).toBeTruthy();
    expect(call.mock.calls.map(([action])=>action)).toEqual(['ui_layout_get','ui_layout_get','browser_native_hide','computer_view_state']);
  });
  it('retains a failed Hide barrier and never treats a preference acknowledgment as visibility proof',async()=>{
    const{call,replies}=bridge({ui_layout_get:{...DEFAULT_LAYOUT,browser_mode:'desktop'},browser_native_hide:{ok:false}});
    render(<Harness/>);await tick();expect(screen.getByText(/browser could not be hidden/)).toBeTruthy();
    expect(call.mock.calls.some(([action])=>action==='computer_view_state')).toBe(false);
    replies.browser_native_hide={ok:true};fireEvent.click(screen.getByRole('button',{name:'Retry switching'}));await tick();
    expect(screen.getByRole('region',{name:'Desktop activity'})).toBeTruthy();
  });
  it.each(['success','failure'])('does not apply an older preference %s after a transport change',async outcome=>{
    const old=deferred();bridge({ui_layout_get:old.promise});
    const view=render(<Harness/>);await tick();
    const current=bridge({ui_layout_get:{...DEFAULT_LAYOUT,browser_mode:'browser'}});
    view.rerender(<Harness/>);await act(async()=>window.dispatchEvent(new Event('pywebviewready')));await tick();
    await act(async()=>old.resolve(outcome==='success'?{...DEFAULT_LAYOUT,browser_mode:'desktop'}:{error:'Synthetic retired failure'}));await tick();
    expect(screen.queryByRole('region',{name:'Desktop activity'})).toBeNull();
    expect(current.call.mock.calls.some(([action])=>action==='computer_view_state')).toBe(false);
    expect(layout.ready).toBe(true);expect(layout.error).toBeNull();expect(layout.layout.browser_mode).toBe('browser');
  });
  it('drains an admitted same-row write before the new transport reads or confirms that preference',async()=>{
    const writing=deferred();const old=bridge({ui_layout_save:writing.promise});const view=render(<Harness/>);await tick();
    fireEvent.click(screen.getByRole('button',{name:'Desktop'}));await tick(350);
    expect(old.call.mock.calls.filter(([action])=>action==='ui_layout_save')).toHaveLength(1);
    const reading=deferred(),current=bridge({ui_layout_get:reading.promise});view.rerender(<Harness/>);await tick();
    expect(layout.ready).toBe(false);expect(current.call.mock.calls.some(([action])=>action==='ui_layout_get')).toBe(false);
    let result;await act(async()=>{expect(updateReadiness.start('view-fixture',vi.fn())).toBe(true);result=await updateReadiness.confirm('view-fixture');updateReadiness.aborted('view-fixture');});
    expect(result).toEqual({ok:false,reason:'preferences_pending'});
    await act(async()=>writing.resolve({...DEFAULT_LAYOUT,browser_mode:'desktop'}));await tick();
    expect(current.call.mock.calls.filter(([action])=>action==='ui_layout_get')).toHaveLength(1);
    expect(layout.ready).toBe(false);expect(screen.queryByRole('region',{name:'Desktop activity'})).toBeNull();
    await act(async()=>reading.resolve({...DEFAULT_LAYOUT,browser_mode:'desktop'}));await tick();
    expect(layout.ready).toBe(true);expect(layout.error).toBeNull();expect(screen.getByRole('region',{name:'Desktop activity'})).toBeTruthy();
    expect(current.call.mock.calls.some(([action])=>action==='ui_layout_save')).toBe(false);
  });
  it('keeps an explicit current-client view choice across a new load and confirms it through that bridge',async()=>{
    bridge();const view=render(<Harness/>);await tick();fireEvent.click(screen.getByRole('button',{name:'Desktop'}));await tick(350);
    const current=bridge({ui_layout_get:{...DEFAULT_LAYOUT,browser_mode:'browser'}});view.rerender(<Harness/>);await tick();await tick(350);
    expect(layout.layout.browser_mode).toBe('desktop');expect(layout.error).toBeNull();
    expect(current.call).toHaveBeenCalledWith('ui_layout_save',{client_id:'desktop',layout:{...DEFAULT_LAYOUT,browser_mode:'desktop'}});
    expect(current.call.mock.calls.some(([action])=>action==='browser_native_show' || action==='browser_native_status')).toBe(false);
  });
  it('retires an explicit browser-client view choice before restoring the native client preference',async()=>{
    delete window.pywebview;
    vi.stubGlobal('fetch',vi.fn(async(url:string,request:RequestInit)=>{
      const data=JSON.parse(String(request.body));return {ok:true,json:async()=>url.endsWith('ui_layout_get')?DEFAULT_LAYOUT:url.endsWith('ui_layout_save')?data.layout:desktop};
    }));const view=render(<Harness/>);await tick();fireEvent.click(screen.getByRole('button',{name:'Desktop'}));await tick(350);
    expect(layout.layout.browser_mode).toBe('desktop');
    const current=bridge();view.rerender(<Harness/>);await act(async()=>window.dispatchEvent(new Event('pywebviewready')));await tick(350);
    expect(layout.layout.browser_mode).toBe('browser');expect(layout.ready).toBe(true);
    expect(current.call.mock.calls.some(([action])=>action==='ui_layout_save' || action==='computer_view_state')).toBe(false);
    expect(current.call).toHaveBeenCalledWith('ui_layout_get',{client_id:'desktop'});
  });
  it('requires the current transport Hide even when a retired Hide completes positively',async()=>{
    const oldHide=deferred();bridge({browser_native_hide:oldHide.promise});const view=render(<BrowserPanel chatId="first" browserMode="desktop"/>);await tick();
    const newHide=deferred(),current=bridge({browser_native_hide:newHide.promise});view.rerender(<BrowserPanel chatId="first" browserMode="desktop"/>);await tick();
    await act(async()=>oldHide.resolve({ok:true}));await tick();
    expect(screen.queryByRole('region',{name:'Desktop activity'})).toBeNull();expect(current.call.mock.calls.map(([action])=>action)).toEqual(['browser_native_hide']);
    await act(async()=>newHide.resolve({ok:true}));await tick();expect(screen.getByRole('region',{name:'Desktop activity'})).toBeTruthy();
  });
  it('drains the retired pane before confirming Desktop through the replacement bridge',async()=>{
    const showing=deferred();bridge({browser_native_show:showing.promise});const view=render(<BrowserPanel chatId="first"/>);await tick();
    const hidden=deferred(),current=bridge({browser_native_hide:hidden.promise});view.rerender(<BrowserPanel chatId="first"/>);
    fireEvent.click(screen.getByRole('button',{name:'Desktop'}));await tick();
    expect(screen.queryByRole('region',{name:'Desktop activity'})).toBeNull();
    await act(async()=>showing.resolve({ok:true}));await tick();expect(screen.queryByRole('region',{name:'Desktop activity'})).toBeNull();
    await act(async()=>hidden.resolve({ok:true}));await tick();expect(screen.getByRole('region',{name:'Desktop activity'})).toBeTruthy();
    expect(current.call.mock.calls.some(([action])=>action==='browser_native_show')).toBe(false);
  });
  it('withholds preference changes while readiness owns the editor',async()=>{
    bridge();render(<Harness/>);await tick();
    act(()=>{expect(updateReadiness.start('view-fixture',vi.fn())).toBe(true);});
    expect((screen.getByRole('button',{name:'Desktop'}) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole('button',{name:'Desktop'}));expect(layout.layout.browser_mode).toBe('browser');
  });
});
