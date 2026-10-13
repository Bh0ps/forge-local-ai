import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { DEFAULT_LAYOUT, useWorkspaceLayout } from '../workspaceLayout';
const deferred=<T,>()=>{let resolve!:(value:T)=>void;const promise=new Promise<T>(done=>{resolve=done;});return {promise,resolve};};
function Harness(){const value=useWorkspaceLayout(true,()=>{});return <><span>{value.layout.sidebar_width}</span><button onClick={()=>value.update({sidebar_width:300})}>First width</button><button onClick={()=>value.update({sidebar_width:340})}>Second width</button></>;}
afterEach(()=>{delete window.pywebview;vi.unstubAllGlobals();});
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
});
