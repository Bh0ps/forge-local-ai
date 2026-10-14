import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ComputerView, type ComputerViewState } from '../computerView';
import { updateReadiness } from '../updateReadiness';

const picture='data:image/png;base64,aGVsbG8=';
const evidence=(chat_id='first',scope_run_id:string|null=null,url=picture):ComputerViewState=>({chat_id,scope_run_id,mode:'shared_desktop',run_id:'own-run',invocation_id:'own-run:2',tool:'computer_inspect',status:'completed',action_count:1,action_count_truncated:false,evidence_mode:'image',failure:null,
  snapshot:{run_id:'own-run',invocation_id:'own-run:2',data_url:url,mime_type:'image/png',width:1600,height:900,age_seconds:4}});
async function tick(ms=1){await act(async()=>{await vi.advanceTimersByTimeAsync(ms);});}
function bridge(response:()=>unknown=()=>evidence()){
  const call=vi.fn(async(action:string)=>{if(action!=='computer_view_state')throw new Error('No capture, input, download or file action is allowed');return response();});
  window.pywebview={api:{call}};return call;
}
beforeEach(()=>{
  vi.useFakeTimers();
  vi.spyOn(HTMLDialogElement.prototype,'showModal').mockImplementation(function(this:HTMLDialogElement){this.open=true;this.querySelector<HTMLButtonElement>('button')?.focus();});
});
afterEach(()=>{cleanup();updateReadiness.aborted('snapshot-fixture');updateReadiness.dismiss();vi.clearAllTimers();vi.useRealTimers();vi.restoreAllMocks();delete window.pywebview;});
function enlarge(){const button=screen.getByRole('button',{name:'Enlarge earlier snapshot'});button.focus();fireEvent.click(button);return button;}
describe('saved desktop snapshot readability',()=>{
  it('uses the same saved pixels in Fit and 100% without an extra request',async()=>{
    const call=bridge();render(<ComputerView chatId="first" onActivity={()=>{}}/>);await tick();const trigger=enlarge();
    const dialog=screen.getByRole('dialog',{name:'Earlier desktop snapshot'});
    expect(screen.getByRole('img',{name:'Earlier shared-desktop snapshot enlarged'}).getAttribute('src')).toBe(picture);
    expect(screen.getByRole('button',{name:'Fit'}).getAttribute('aria-pressed')).toBe('true');
    fireEvent.click(screen.getByRole('button',{name:'100%'}));
    expect(screen.getByRole('region',{name:'Earlier snapshot image'}).className).toContain('actual-size');
    fireEvent.click(screen.getByRole('button',{name:'Fit'}));
    expect(screen.getByRole('region',{name:'Earlier snapshot image'}).className).not.toContain('actual-size');
    fireEvent(dialog,new Event('cancel',{bubbles:true,cancelable:true}));
    expect(screen.queryByRole('dialog')).toBeNull();expect(document.activeElement).toBe(trigger);
    expect(call.mock.calls.map(([action])=>action)).toEqual(['computer_view_state']);
  });
  it('keeps a reading dialog open when only saved evidence age changes',async()=>{
    let age=4;bridge(()=>({...evidence(),snapshot:{...evidence().snapshot!,age_seconds:age}}));
    render(<ComputerView chatId="first" onActivity={()=>{}}/>);await tick();enlarge();fireEvent.click(screen.getByRole('button',{name:'100%'}));
    age=6;await tick(1500);
    expect(screen.getByRole('dialog',{name:'Earlier desktop snapshot'})).toBeTruthy();
    expect(screen.getByRole('region',{name:'Earlier snapshot image'}).className).toContain('actual-size');
  });
  it.each(['chat','run','transport','pixels','hidden'] as const)('retires the dialog on %s change without stealing other-control focus',async change=>{
    let current=evidence();bridge(()=>current);
    const view=render(<><button>Other control</button><ComputerView chatId="first" onActivity={()=>{}}/></>);await tick();enlarge();
    const other=screen.getByRole('button',{name:'Other control'});other.focus();
    if(change==='chat'){current=evidence('second');view.rerender(<><button>Other control</button><ComputerView chatId="second" onActivity={()=>{}}/></>);}
    else if(change==='run'){current=evidence('first','new-run');view.rerender(<><button>Other control</button><ComputerView chatId="first" runId="new-run" onActivity={()=>{}}/></>);}
    else if(change==='transport'){bridge(()=>current);view.rerender(<><button>Other control</button><ComputerView chatId="first" onActivity={()=>{}}/></>);}
    else if(change==='hidden')view.rerender(<><button>Other control</button><ComputerView chatId="first" visible={false} onActivity={()=>{}}/></>);
    else {current=evidence('first',null,'data:image/png;base64,d29ybGQ=');await tick(1500);}
    await tick();expect(screen.queryByRole('dialog')).toBeNull();expect(document.activeElement).toBe(other);
  });
  it('does not restore focus to a repurposed image button after changed pixels',async()=>{
    let current=evidence();bridge(()=>current);render(<ComputerView chatId="first" onActivity={()=>{}}/>);await tick();const trigger=enlarge();
    current=evidence('first',null,'data:image/png;base64,d29ybGQ=');await tick(1500);
    expect(screen.queryByRole('dialog')).toBeNull();expect(document.activeElement).not.toBe(trigger);
  });
  it('withholds enlargement during readiness and never enlarges accessibility-only evidence',async()=>{
    bridge();const view=render(<ComputerView chatId="first" onActivity={()=>{}}/>);await tick();
    act(()=>{expect(updateReadiness.start('snapshot-fixture',vi.fn())).toBe(true);});
    expect((screen.getByRole('button',{name:'Enlarge earlier snapshot'}) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole('button',{name:'Enlarge earlier snapshot'}));expect(screen.queryByRole('dialog')).toBeNull();
    updateReadiness.aborted('snapshot-fixture');bridge(()=>({...evidence(),evidence_mode:'accessibility_only',snapshot:null}));
    view.rerender(<ComputerView chatId="first" onActivity={()=>{}}/>);await tick();
    expect(screen.queryByRole('button',{name:'Enlarge earlier snapshot'})).toBeNull();
  });
});
