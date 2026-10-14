import { useState } from 'react';
import { act,cleanup,fireEvent,render,screen,waitFor,within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach,describe,expect,it,vi } from 'vitest';
import App from '../App';
import { ComputerView,type ComputerViewState } from '../computerView';
import { WorkspacePanel,type WorkspacePanelFocusScope } from '../workspacePanel';
import { DEFAULT_SETTINGS } from '../types';
import { DEFAULT_LAYOUT } from '../workspaceLayout';
import { updateReadiness } from '../updateReadiness';

const attempt='a'.repeat(32);
afterEach(()=>{cleanup();updateReadiness.aborted(attempt);updateReadiness.dismiss();vi.restoreAllMocks();});
const evidence=(chatId='focus-chat',runId?:string):ComputerViewState=>({chat_id:chatId,scope_run_id:runId||null,mode:'shared_desktop',run_id:runId||null,invocation_id:'focus-run:0',tool:'computer_click',status:'completed',action_count:1,action_count_truncated:false,evidence_mode:'none',snapshot:null,failure:null});
function pane(options:{after?:()=>void;deferred?:boolean;scope?:Partial<WorkspacePanelFocusScope>}={}){
  const call=vi.fn(async(action:string,data:Record<string,unknown>={})=>action==='computer_view_state'?evidence(String(data.chat_id),data.run_id as string|undefined):{});window.pywebview={api:{call}};
  let change!:(scope:Partial<WorkspacePanelFocusScope>)=>void,complete!:()=>void,reopen!:()=>void,close!:()=>void;
  function Harness(){
    const [tab,setTab]=useState('browser'),[open,setOpen]=useState(true),[changed,setChanged]=useState<Partial<WorkspacePanelFocusScope>>({});
    const scope:WorkspacePanelFocusScope={page:'chat',projectId:null,chatId:'focus-chat',runId:'focus-run',navigation:0,transport:call,visible:true,current:()=>true,...options.scope,...changed};
    change=setChanged;complete=()=>setTab('activity');reopen=()=>{setOpen(true);setTab('browser');};close=()=>setOpen(false);
    return <><input aria-label="Other focus target"/>{open&&<WorkspacePanel tab={tab} focusScope={scope} select={setTab} close={close} width={320} resize={()=>{}}>
      {tab==='browser'?<ComputerView chatId={scope.chatId} runId={scope.runId} visible={scope.visible} onActivity={()=>{options.after?.();if(!options.deferred)setTab('activity');}}/>:<p>Current activity details</p>}
    </WorkspacePanel>}</>;
  }
  render(<Harness/>);return {call,change:(scope:Partial<WorkspacePanelFocusScope>)=>change(scope),complete:()=>complete(),reopen:()=>reopen(),close:()=>close()};
}
async function activate(){const button=await screen.findByRole('button',{name:'Activity'});button.focus();expect(document.activeElement).toBe(button);fireEvent.click(button);}
const selectedTab=()=>screen.getByRole('tab',{name:'Activity'});

describe('one event-owned pane focus transfer',()=>{
  it('focuses the existing Activity tab only when the focused desktop button retires',async()=>{
    pane();await activate();expect(document.activeElement).toBe(selectedTab());expect(selectedTab().getAttribute('aria-selected')).toBe('true');
  });
  it('preserves pointer selection without claiming focus from an unfocused programmatic button',async()=>{
    pane();const button=await screen.findByRole('button',{name:'Activity'});screen.getByLabelText('Other focus target').focus();fireEvent.click(button);
    expect(selectedTab().getAttribute('aria-selected')).toBe('true');expect(document.activeElement).toBe(screen.getByLabelText('Other focus target'));
  });
  it('consumes delayed selection rather than focusing after a later programmatic switch',async()=>{
    const owner=pane({deferred:true});await activate();expect(screen.getByRole('tab',{name:'Browser'}).getAttribute('aria-selected')).toBe('true');
    await act(async()=>owner.complete());expect(document.activeElement).toBe(document.body);expect(selectedTab().getAttribute('aria-selected')).toBe('true');
  });
  it.each(['chatId','runId','projectId','navigation','transport'] as const)('rejects a pending request after %s changes',async field=>{
    let owner!:ReturnType<typeof pane>;owner=pane({after:()=>owner.change({[field]:field==='navigation'?1:field==='transport'?async()=>({}):'replacement'})});
    await activate();expect(selectedTab().getAttribute('aria-selected')).toBe('true');expect(document.activeElement).toBe(document.body);
  });
  it('does not replace a control focused during the route',async()=>{
    pane({after:()=>screen.getByLabelText('Other focus target').focus()});await activate();expect(document.activeElement).toBe(screen.getByLabelText('Other focus target'));
  });
  it.each(['wheel','pointerdown','keydown','blur','native-hide-show','document-hide','transport-ready','navigation'])('retires intent after %s without replay',async boundary=>{
    let hidden=false;if(boundary==='document-hide')vi.spyOn(document,'visibilityState','get').mockImplementation(()=>hidden?'hidden':'visible');
    const owner=pane({after:()=>{
      if(boundary==='native-hide-show'){window.dispatchEvent(new CustomEvent('forge:visibility',{detail:{visible:false,seq:10}}));window.dispatchEvent(new CustomEvent('forge:visibility',{detail:{visible:true,seq:11}}));}
      else if(boundary==='document-hide'){hidden=true;document.dispatchEvent(new Event('visibilitychange'));}
      else if(['wheel','pointerdown','keydown'].includes(boundary))document.dispatchEvent(new Event(boundary,{bubbles:true}));
      else window.dispatchEvent(new Event(boundary==='transport-ready'?'pywebviewready':boundary==='navigation'?'forge:navigate':'blur'));
    }});
    await activate();expect(document.activeElement).toBe(document.body);
    hidden=false;await act(async()=>owner.change({visible:true}));expect(document.activeElement).toBe(document.body);
  });
  it('does not replay after update locking and unlocking',async()=>{
    const owner=pane({after:()=>expect(updateReadiness.start(attempt,()=>{})).toBe(true)});await activate();expect(document.activeElement).toBe(document.body);
    await act(async()=>updateReadiness.aborted(attempt));await act(async()=>owner.change({visible:true}));expect(document.activeElement).toBe(document.body);
  });
  it('refuses admission while hidden or locked',async()=>{
    const owner=pane();await screen.findByRole('button',{name:'Activity'});
    await act(async()=>owner.change({visible:false}));expect(screen.queryByRole('button',{name:'Activity'})).toBeNull();expect(screen.getByRole('tab',{name:'Browser'}).getAttribute('aria-selected')).toBe('true');
    await act(async()=>owner.change({visible:true}));await screen.findByRole('button',{name:'Activity'});
    expect(updateReadiness.start(attempt,()=>{})).toBe(true);await activate();expect(screen.getByRole('tab',{name:'Browser'}).getAttribute('aria-selected')).toBe('true');
  });
  it('drops an unmounted pane request and does not focus a reopened pane',async()=>{
    let owner!:ReturnType<typeof pane>;owner=pane({after:()=>owner.close()});await activate();expect(screen.queryByRole('complementary',{name:'Workspace details'})).toBeNull();
    await act(async()=>owner.reopen());await screen.findByRole('button',{name:'Activity'});expect(document.activeElement).toBe(document.body);
  });
});

async function app(){
  const chat={id:'focus-chat',project_id:null,title:'Focus conversation',messages:[{id:1,role:'user',content:'Synthetic request'},{id:2,role:'assistant',content:'Saved focus evidence'}]};
  const drafts=new Map<string,{revision:number;content:unknown;scope_available:boolean}>();
  const call=vi.fn(async(action:string,data:Record<string,unknown>={})=>{
    if(action==='bootstrap')return {settings:{...DEFAULT_SETTINGS,model:'fixture'},projects:[],chats:[chat],runs:[],capabilities:{}};
    if(action==='get_chat')return chat;if(action==='setup_status')return {first_run:false};
    if(action==='ui_layout_get')return {...DEFAULT_LAYOUT,right_panel_open:true,right_panel_tab:'browser',browser_mode:'desktop'};
    if(action==='ui_layout_save')return data.layout;
    if(action==='browser_native_hide')return {ok:true};
    if(action==='computer_view_state')return evidence('focus-chat',undefined);
    if(action==='host_status')return {visible:true,visibility_seq:0};
    if(action==='pending_questions')return {questions:[]};
    const key=JSON.stringify([data.scope_kind,data.chat_id,data.project_id]);
    const current=drafts.get(key)||{revision:0,content:data.chat_id===chat.id?{text:'Retained focus draft'}:{},scope_available:true};
    if(action==='draft_get')return current;
    if(action==='draft_save'){const next={revision:Number(data.expected_revision)+1,content:data.content,scope_available:true};drafts.set(key,next);return next;}
    if(['models','projects','chats','runs','spaces','schedules'].includes(action))return {[action]:action==='chats'?[chat]:[]};
    return {};
  });window.pywebview={api:{call}};render(<App/>);await act(async()=>window.dispatchEvent(new Event('pywebviewready')));await screen.findByText('Saved focus evidence');await screen.findByRole('button',{name:'Activity'});
  await waitFor(()=>expect((screen.getByRole('textbox',{name:'Message Forge'}) as HTMLTextAreaElement).value).toBe('Retained focus draft'));return {call};
}
describe('actual App desktop Activity entry',()=>{
  it('retains the draft while Enter moves focus into the existing selected Activity tab',async()=>{
    const {call}=await app();const activity=screen.getByRole('button',{name:'Activity'});activity.focus();await userEvent.keyboard('{Enter}');
    expect(document.activeElement).toBe(selectedTab());expect(selectedTab().getAttribute('aria-selected')).toBe('true');expect(screen.getByRole('region',{name:'Workspace activity'})).toBeTruthy();
    expect((screen.getByRole('textbox',{name:'Message Forge'}) as HTMLTextAreaElement).value).toBe('Retained focus draft');
    expect(call.mock.calls.some(([action])=>['command','start','resume','computer_click','computer_inspect'].includes(action))).toBe(false);
  });
  it('preserves ArrowRight/Home/End tab focus navigation',async()=>{
    await app();const tabs=screen.getByRole('tablist',{name:'Detail pane views'});const browser=within(tabs).getByRole('tab',{name:'Browser'});browser.focus();
    await userEvent.keyboard('{ArrowRight}');expect(document.activeElement).toBe(within(tabs).getByRole('tab',{name:'Activity'}));
    await userEvent.keyboard('{Home}');expect(document.activeElement).toBe(within(tabs).getByRole('tab',{name:'Context'}));
    await userEvent.keyboard('{End}');expect(document.activeElement).toBe(within(tabs).getByRole('tab',{name:'Files'}));
  });
});
