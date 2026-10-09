import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import App from '../App';
import { BuilderPage, type BuilderBrief } from '../BuilderPage';
import { OpenRouterSettings } from '../openRouterSettings';
import { WorkspaceSidebar } from '../workspaceSidebar';
import { SchedulesPage } from '../pages';
import { DEFAULT_SETTINGS } from '../types';
const chat={id:'chat',title:'Original chat',model:'fixture',project_id:null,messages:[]};
function deferred<T=unknown>(){let resolve!:(value:T)=>void; const promise=new Promise<T>(done=>{resolve=done;});return {promise,resolve};}
function bridge(overrides:Record<string,unknown>={}){
  const responses:Record<string,unknown>={bootstrap:{settings:{...DEFAULT_SETTINGS,model:'fixture'},projects:[],chats:[chat],runs:[],capabilities:{}},get_chat:chat,projects:{projects:[]},chats:{chats:[chat]},models:{models:[{name:'fixture'}]},runs:{runs:[]},spaces:{spaces:[]},schedules:{schedules:[]},pending_questions:{questions:[]},setup_status:{first_run:false},draft_get:{revision:0,content:{}},draft_save:({content}:Record<string,unknown>)=>({revision:1,content}),draft_clear:{revision:2,content:{}},...overrides};
  const call=vi.fn(async(action:string,data:Record<string,unknown>={})=>{const result=responses[action]; if(result===undefined)throw new Error(`Unexpected ${action}`);return typeof result==='function'?result(data):result;});window.pywebview={api:{call}};return call;
}
async function app(){render(<App/>);await act(async()=>window.dispatchEvent(new Event('pywebviewready')));await screen.findByRole('button',{name:'Attach files'});await waitFor(()=>expect((screen.getByRole('textbox',{name:'Message Forge'}) as HTMLTextAreaElement).value).toBe(''));}
const input=()=>screen.getByRole('textbox',{name:'Message Forge'}) as HTMLTextAreaElement;

describe('Forge 5.0.1 submission and drafts',()=>{
  it('does not redirect navigation or erase newer drafts after a delayed send',async()=>{
    const pending=deferred();const call=bridge({start_chat:()=>pending.promise});await app();
    fireEvent.change(input(),{target:{value:'Submitted request'}});fireEvent.click(screen.getByRole('button',{name:'Send message'}));
    await waitFor(()=>expect(call.mock.calls.some(([name])=>name==='start_chat')).toBe(true));
    fireEvent.click(screen.getByRole('button',{name:/New chat/}));fireEvent.change(input(),{target:{value:'My newer conversation draft'}});
    await act(async()=>pending.resolve({id:'new-run',chat_id:'chat',project_id:null,status:'running'}));
    expect(input().value).toBe('My newer conversation draft');expect(screen.getByRole('button',{name:'Send message'})).toBeTruthy();
    expect(call).toHaveBeenCalledWith('start_chat',expect.objectContaining({chat_id:'chat',text:'Submitted request',client_submission_id:expect.any(String)}));
  });
  it('keeps a newer draft in the same chat while a response and draft cleanup finish',async()=>{
    const pending=deferred();const call=bridge({start_chat:()=>pending.promise});await app();fireEvent.change(input(),{target:{value:'First'}});fireEvent.click(screen.getByRole('button',{name:'Send message'}));
    await waitFor(()=>expect(call.mock.calls.some(([name])=>name==='start_chat')).toBe(true));fireEvent.change(input(),{target:{value:'Newer text'}});
    await act(async()=>pending.resolve({id:'run',chat_id:'chat',status:'running'}));expect(input().value).toBe('Newer text');
    expect(call.mock.calls.some(([name])=>name==='draft_clear')).toBe(false);
  });
  it('retains failed submissions and reuses their admission key',async()=>{
    let count=0;const call=bridge({start_chat:()=>++count===1?{error:'Uncertain network response'}:{id:'run',chat_id:'chat',status:'running'},submission_get:{state:'unknown',admission_error:'No confirmed admission'}});await app();
    fireEvent.change(input(),{target:{value:'Keep this request'}});fireEvent.keyDown(input(),{key:'Enter'});await screen.findByText('Uncertain network response');expect(input().value).toBe('Keep this request');
    await userEvent.click(screen.getByRole('button',{name:'Inspect submission status'}));await screen.findByText('Submission: unknown');expect(call).toHaveBeenCalledWith('submission_get',expect.objectContaining({action:'start_chat',chat_id:'chat',client_submission_id:expect.any(String)}));expect(input().value).toBe('Keep this request');
    fireEvent.keyDown(input(),{key:'Enter'});await waitFor(()=>expect(input().value).toBe(''));
    const sent=call.mock.calls.filter(([name])=>name==='start_chat').map(([,data])=>data);expect(sent).toHaveLength(2);expect(sent[0]!.client_submission_id).toBe(sent[1]!.client_submission_id);
  });
  it('preserves typing that races draft restoration and uses its server revision',async()=>{
    const draft=deferred();const call=bridge({draft_get:()=>draft.promise});await app();fireEvent.change(input(),{target:{value:'Typed before restoration'}});
    await act(async()=>draft.resolve({revision:8,content:{text:'Older saved text',document_ids:[],skills:[],space_ids:[]}}));expect(input().value).toBe('Typed before restoration');
    await waitFor(()=>expect(call).toHaveBeenCalledWith('draft_save',expect.objectContaining({chat_id:'chat',expected_revision:8,content:expect.objectContaining({text:'Typed before restoration'})})));
  });
  it('restores saved text and references without persisting image payloads',async()=>{
    const call=bridge({draft_get:{revision:3,content:{text:'Saved text',document_ids:['doc'],skills:['skill'],space_ids:['space']},stale_refs:[{id:'doc',reason:'Missing document'}]}});render(<App/>);await act(async()=>window.dispatchEvent(new Event('pywebviewready')));
    await waitFor(()=>expect(input().value).toBe('Saved text'));expect(screen.getByText('Saved document')).toBeTruthy();expect(screen.getByText('Draft restored; some references are unavailable')).toBeTruthy();
    expect(call.mock.calls.filter(([name])=>name==='draft_save').every(([,data])=>!('images' in (data!.content as object)))).toBe(true);
  });
  it('shows draft conflicts while preserving local edits',async()=>{
    bridge({draft_save:{error:'Draft changed in another window. Reload or keep your local edits.'}});await app();fireEvent.change(input(),{target:{value:'Local unsaved work'}});
    await screen.findByText(/Draft kept in this window/);expect(input().value).toBe('Local unsaved work');
  });
  it('discards conflicting local edits without deleting another window\'s saved draft',async()=>{
    let reads=0;const call=bridge({draft_get:(data:Record<string,unknown>)=>data.scope_kind!=='chat'||++reads===1?{revision:0,content:{}}:{revision:9,content:{text:'Other window work'}},draft_save:{error:'Draft changed in another window. Reload or keep your local edits.'}});await app();fireEvent.change(input(),{target:{value:'Local conflicting work'}});await screen.findByText(/Draft kept in this window/);
    fireEvent.click(screen.getByRole('button',{name:'Discard draft'}));await waitFor(()=>expect(input().value).toBe('Other window work'));expect(call.mock.calls.some(([name])=>name==='draft_clear')).toBe(false);
  });
  it('blocks pending uploads and attaches their result to the captured chat',async()=>{
    const pending=deferred();const call=bridge({document_upload:()=>pending.promise});await app();fireEvent.change(input(),{target:{value:'Inspect this file'}});
    const file=new File(['data'],'notes.txt',{type:'text/plain'});fireEvent.change(document.querySelector('input[type=file]')!,{target:{files:[file]}});
    await waitFor(()=>expect(call.mock.calls.some(([name])=>name==='document_upload')).toBe(true));expect(screen.getByRole('button',{name:'Send message'})).toHaveProperty('disabled',true);
    fireEvent.click(screen.getByRole('button',{name:/New chat/}));await act(async()=>pending.resolve({id:'uploaded-doc'}));expect(screen.queryByText('notes.txt')).toBeNull();
    fireEvent.click(screen.getByRole('button',{name:'Original chat'}));expect(await screen.findByText('notes.txt')).toBeTruthy();
    expect(call.mock.calls.some(([name])=>name==='start_chat')).toBe(false);
  });
  it('handles slash selection and IME Enter without accidental submission',async()=>{
    const call=bridge();await app();fireEvent.change(input(),{target:{value:'/st'}});fireEvent.keyDown(input(),{key:'ArrowDown'});fireEvent.keyDown(input(),{key:'Enter'});
    expect(input().value).toMatch(/^\/\w+ $/);expect(call.mock.calls.some(([name])=>name==='command')).toBe(false);
    fireEvent.change(input(),{target:{value:'Composing'}});fireEvent.keyDown(input(),{key:'Enter',isComposing:true});expect(call.mock.calls.some(([name])=>name==='start_chat')).toBe(false);
  });
});

const brief:BuilderBrief={id:'b',project_id:'p',title:'Fixture',objective:'Build a page',audience:'Readers',constraints:'',revision:4,status:'draft',requirements:[{id:'requirement',text:'A useful page',acceptance:'Works'}],gates:{functional:{status:'pending'}},previews:[]};
function builderBridge(extra:Record<string,unknown>={}){return bridge({builder_list:{builders:[brief]},builder_get:brief,builder_runtime_get:{...brief,freshness_note:'Recorded checks only.'},builder_start_goal:{id:'r',goal_id:'g',chat_id:'c',status:'running'},...extra});}
describe('Forge 5.0.1 Builder state',()=>{
  it('does not show an unrelated ordinary run as a Builder attempt',async()=>{
    builderBridge();render(<BuilderPage projects={[{id:'p',name:'Project',path:'/project'}]} runs={[{id:'other',status:'paused',planner_assignment:{state:'consumed',run_id:'unrelated'}}]}/>);await screen.findByLabelText('Objective');expect(screen.queryByText('OpenRouter: Guidance supplied to local execution')).toBeNull();expect(screen.queryByText(/Goal:.*paused/)).toBeNull();
  });
  it('builds an unchanged saved revision without saving and tracks the returned run',async()=>{
    const call=builderBridge(),onRun=vi.fn();render(<BuilderPage projects={[{id:'p',name:'Project',path:'/project'}]} onRun={onRun}/>);await screen.findByLabelText('Objective');await userEvent.click(screen.getByRole('button',{name:'Build'}));
    await waitFor(()=>expect(onRun).toHaveBeenCalledWith(expect.objectContaining({id:'r'})));expect(call.mock.calls.some(([name])=>name==='builder_save')).toBe(false);
    expect(call).toHaveBeenCalledWith('builder_start_goal',expect.objectContaining({id:'b',expected_revision:4,client_submission_id:expect.any(String)}));
  });
  it('preserves editable input during recorded runtime refresh and guards navigation',async()=>{
    builderBridge();render(<BuilderPage projects={[{id:'p',name:'Project',path:'/project'}]}/>);await screen.findByLabelText('Objective');fireEvent.change(screen.getByLabelText('Objective'),{target:{value:'Local edited objective'}});
    const navigate=new CustomEvent('forge:navigate',{cancelable:true,detail:{page:'projects'}});await act(async()=>window.dispatchEvent(navigate));expect(navigate.defaultPrevented).toBe(true);
    expect(screen.getByRole('button',{name:'Stay'})).toBeTruthy();await userEvent.click(screen.getByRole('button',{name:'Stay'}));expect(screen.getByLabelText('Objective')).toHaveProperty('value','Local edited objective');
  });
  it('shows failed preview errors and a restart action',async()=>{
    const failed={id:'failed',status:'failed',error:'Port occupied'};builderBridge({builder_get:{...brief,previews:[failed]},builder_runtime_get:{...brief,previews:[failed]}});render(<BuilderPage projects={[{id:'p',name:'Project',path:'/project'}]}/>);await screen.findByLabelText('Objective');await userEvent.click(screen.getByRole('button',{name:'Preview'}));
    expect(screen.getByText('Preview failed')).toBeTruthy();expect(screen.getByText('Port occupied')).toBeTruthy();expect(screen.getByRole('button',{name:'Restart preview'})).toBeTruthy();
  });
  it('reloads browser previews by replacing the iframe, without native host operations',async()=>{
    const ready={id:'preview',status:'ready',url:'http://127.0.0.1:1234'};const call=builderBridge({builder_get:{...brief,previews:[ready]},builder_runtime_get:{...brief,previews:[ready]}});delete window.pywebview;
    vi.spyOn(window,'fetch').mockImplementation(async(url,options)=>{const action=String(url).split('/').at(-1)!;const value=await call(action,JSON.parse(String(options?.body||'{}')));return new Response(JSON.stringify(value),{status:200,headers:{'Content-Type':'application/json'}});});
    render(<BuilderPage projects={[{id:'p',name:'Project',path:'/project'}]}/>);await screen.findByLabelText('Objective');await userEvent.click(screen.getByRole('button',{name:'Preview'}));const original=screen.getByTitle('Local application preview');
    await userEvent.click(screen.getByRole('button',{name:'Reload preview'}));expect(screen.getByTitle('Local application preview')).not.toBe(original);expect(call.mock.calls.some(([name])=>name==='preview_browser_reload')).toBe(false);
  });
});

describe('Forge 5.0.1 connection and navigation clarity',()=>{
  it('updates Scheduled counts from enabled schedules, independently from active chats',async()=>{
    bridge({schedules:{schedules:[{id:'one',name:'Daily review',prompt:'Review',enabled:true},{id:'two',name:'Paused review',prompt:'Review',enabled:false},{id:'three',name:'Research',prompt:'Read',enabled:true}]},agents:{agents:[]}});const count=vi.fn();render(<SchedulesPage projects={[]} notify={()=>{}} onRun={()=>{}} onCount={count}/>);await screen.findByText('Daily review');expect(count).toHaveBeenCalledWith(2);
  });
  it('distinguishes authenticated metadata from verified workflow and configures only explicitly',async()=>{
    const connection={enabled:true,connected:true,remote_consent:true,data_collection:'deny'};const status={configuration:{state:'configured'},authentication:{state:'verified'},free_capacity:{state:'available',remaining:10},profiles:{state:'ready'},workflow:{state:'unverified'}};
    const call=bridge({openrouter_status:{provider:connection},ai_workflow_status:status,ai_workflow_setup:{status,settings:{goal_cloud_guidance:true,auto_delegate:true,goal_review_enabled:true}}}),change=vi.fn(async()=>{});
    render(<OpenRouterSettings settings={DEFAULT_SETTINGS} onChange={change} notify={()=>{}}/>);await screen.findByText('Account authenticated');expect(screen.queryByText('Workflow verified')).toBeNull();expect(call.mock.calls.some(([name])=>name==='ai_workflow_setup')).toBe(false);
    await userEvent.click(screen.getByRole('button',{name:'Set up goal connections'}));await waitFor(()=>expect(call).toHaveBeenCalledWith('ai_workflow_setup',{goal_guidance:true}));expect(change).toHaveBeenCalledWith({goal_cloud_guidance:true,auto_delegate:true,goal_review_enabled:true});
  });
  it('expands projects independently from selecting them and reveals additional chats',async()=>{
    const project={id:'p',name:'Project',path:'/project'},select=vi.fn();const chats=Array.from({length:17},(_,i)=>({id:`c${i}`,title:`Chat ${i}`,project_id:'p',model:'fixture'}));
    render(<WorkspaceSidebar page="chat" projects={[project]} chats={chats} projectId="p" chatId={null} search="" onSearch={()=>{}} activeChatIds={['c0']} scheduledCount={2} navItems={[]} onPage={()=>{}} onNew={()=>{}} onCollapse={()=>{}} onConnect={()=>{}} onProject={select} onChat={()=>{}} onChatChanged={async()=>{}} onProjectRemoved={async()=>{}}/>);
    await userEvent.click(screen.getByRole('button',{name:'Collapse Project'}));expect(select).not.toHaveBeenCalled();expect(screen.queryByRole('button',{name:'Chat 0'})).toBeNull();
    await userEvent.click(screen.getByRole('button',{name:'Expand Project'}));await userEvent.click(screen.getByRole('button',{name:'Show more (2)'}));expect(screen.getByRole('button',{name:'Chat 16'})).toBeTruthy();
  });
});
