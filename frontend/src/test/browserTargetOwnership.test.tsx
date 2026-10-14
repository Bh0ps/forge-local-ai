import {act,cleanup,fireEvent,render,screen} from '@testing-library/react';
import {afterEach,beforeEach,expect,it,vi} from 'vitest';
import {BrowserPanel} from '../browserPanel';
import {updateReadiness} from '../updateReadiness';

const initial={running:true,visible:true,url:'https://synthetic.invalid',title:'Synthetic',target_indicator:'active'};
const deferred=()=>{let resolve!:(value:unknown)=>void;const promise=new Promise(done=>{resolve=done;});return {promise,resolve};};
function bridge(overrides:Record<string,unknown>={}){
  const replies:Record<string,unknown>={browser_native_status:initial,browser_native_show:{ok:true},browser_native_hide:{ok:true},emergency_stop:{ok:true},...overrides};
  const call=vi.fn(async(name:string,data:Record<string,unknown>)=>{if(!(name in replies))throw new Error('Unexpected synthetic action');const reply=replies[name];return typeof reply==='function'?reply(data):reply;});
  window.pywebview={api:{call}};return {call,replies};
}
async function tick(ms=1){await act(async()=>{await vi.advanceTimersByTimeAsync(ms);});}
beforeEach(()=>{vi.useFakeTimers();vi.stubGlobal('ResizeObserver',class{observe(){}disconnect(){}});
  vi.stubGlobal('requestAnimationFrame',(callback:FrameRequestCallback)=>setTimeout(()=>callback(performance.now()),1));
  vi.stubGlobal('cancelAnimationFrame',(id:number)=>clearTimeout(id));
  vi.spyOn(Element.prototype,'getBoundingClientRect').mockReturnValue({x:800,y:100,width:400,height:600} as DOMRect);});
afterEach(()=>{cleanup();updateReadiness.aborted('target-fixture');updateReadiness.dismiss();vi.clearAllTimers();vi.useRealTimers();vi.restoreAllMocks();vi.unstubAllGlobals();delete window.pywebview;});

it('reads only the current scope and shows a fixed cue without private target fields',async()=>{
  const {call}=bridge({browser_native_status:{...initial,selector:'private-selector',nonce:'private-nonce',typed_value:'private-typed'}});
  render(<BrowserPanel chatId="chat-a" runId="run-a"/>);await tick();
  expect(call).toHaveBeenCalledWith('browser_native_status',{chat_id:'chat-a',run_id:'run-a'});
  expect(screen.getByRole('status',{name:'AI browser target'}).textContent).toBe('AI target');
  expect(screen.queryByText(/private-/)).toBeNull();expect(screen.getByRole('button',{name:'Stop all agents'})).toBeTruthy();
});
it.each([undefined,'completed',true,null])('requires the literal active indicator (%s)',async target_indicator=>{
  bridge({browser_native_status:{...initial,target_indicator}});render(<BrowserPanel chatId="chat-a" runId="run-a"/>);await tick();
  expect(screen.queryByRole('status',{name:'AI browser target'})).toBeNull();
});
it.each(['chat','run','project','transport'])('ignores an older indicator reply after %s changes',async change=>{
  const old=deferred();const {replies}=bridge({browser_native_status:old.promise});
  const view=render(<BrowserPanel chatId="chat-a" runId="run-a" projectId="project-a"/>);await tick();
  replies.browser_native_status={...initial,target_indicator:'none'};
  if(change==='transport')bridge({browser_native_status:{...initial,target_indicator:'none'}});
  view.rerender(<BrowserPanel chatId={change==='chat'?'chat-b':'chat-a'} runId={change==='run'?'run-b':'run-a'} projectId={change==='project'?'project-b':'project-a'}/>);await tick();
  await act(async()=>old.resolve(initial));await tick();expect(screen.queryByRole('status',{name:'AI browser target'})).toBeNull();
});
it('does not claim target activity for an unselected run or a hidden host',async()=>{
  bridge();const view=render(<BrowserPanel chatId="chat-a"/>);await tick();expect(screen.queryByRole('status',{name:'AI browser target'})).toBeNull();
  view.rerender(<BrowserPanel chatId="chat-a" runId="run-a" visible={false}/>);await tick();expect(screen.queryByRole('status',{name:'AI browser target'})).toBeNull();
});
it('clears the cue when a user navigation is admitted and retains a newer unsent address',async()=>{
  const pending=deferred();bridge({browser_native_navigate:pending.promise});render(<BrowserPanel chatId="chat-a" runId="run-a"/>);await tick();
  expect(screen.getByRole('status',{name:'AI browser target'})).toBeTruthy();const input=screen.getByRole('textbox',{name:'Agent browser address'});
  fireEvent.change(input,{target:{value:'next.invalid'}});fireEvent.submit(input.closest('form')!);
  expect(screen.queryByRole('status',{name:'AI browser target'})).toBeNull();fireEvent.change(input,{target:{value:'retained.invalid'}});
  await act(async()=>pending.resolve({...initial,target_indicator:'none'}));expect((input as HTMLInputElement).value).toBe('retained.invalid');
});
it('keeps emergency stop available while restart ownership holds without unlocking',async()=>{
  const {call}=bridge();render(<BrowserPanel chatId="chat-a" runId="run-a"/>);await tick();
  act(()=>{expect(updateReadiness.start('target-fixture',vi.fn())).toBe(true);});
  fireEvent.click(screen.getByRole('button',{name:'Stop all agents'}));await tick();
  expect(call).toHaveBeenCalledWith('emergency_stop',{});expect(updateReadiness.canEdit()).toBe(false);
  expect(screen.getByText('Stop requested. An action already sent may finish.')).toBeTruthy();
});

it('clears the current cue immediately on Stop and retires an older active status reply',async()=>{
  const pending=deferred();const {replies,call}=bridge();render(<BrowserPanel chatId="chat-a" runId="run-a"/>);await tick();
  expect(screen.getByRole('status',{name:'AI browser target'})).toBeTruthy();
  replies.browser_native_status=pending.promise;await tick(1500);
  fireEvent.click(screen.getByRole('button',{name:'Stop all agents'}));
  expect(screen.queryByRole('status',{name:'AI browser target'})).toBeNull();
  await act(async()=>pending.resolve(initial));await tick();
  expect(screen.queryByRole('status',{name:'AI browser target'})).toBeNull();
  expect(call).toHaveBeenCalledWith('emergency_stop',{});
});

it('expires a prior cue while status is stalled and does not publish a late active snapshot',async()=>{
  let now=0;vi.spyOn(performance,'now').mockImplementation(()=>now);
  const pending=deferred();const {replies}=bridge();render(<BrowserPanel chatId="chat-a" runId="run-a"/>);await tick();
  expect(screen.getByRole('status',{name:'AI browser target'})).toBeTruthy();
  replies.browser_native_status=pending.promise;await tick(5000);
  expect(screen.queryByRole('status',{name:'AI browser target'})).toBeNull();
  // Advance the request clock separately from the fake polling timers: even
  // the latest admitted status reply now exceeds the bounded cue lifetime.
  now=4000;await act(async()=>pending.resolve(initial));await tick();
  expect(screen.queryByRole('status',{name:'AI browser target'})).toBeNull();
});
