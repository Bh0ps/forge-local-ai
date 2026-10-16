import { act,fireEvent,render,screen,waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe,expect,it,vi } from 'vitest';
import App from '../App';
import { consumeWorkflowPrefix,requestMode } from '../composerModes';
import { DEFAULT_SETTINGS } from '../types';
import { DEFAULT_LAYOUT } from '../workspaceLayout';

function deferred<T=unknown>(){let resolve!:(value:T)=>void;const promise=new Promise<T>(yes=>{resolve=yes;});return{resolve,promise};}
const first={id:'first',title:'First conversation',project_id:null,model:'synthetic-model'},second={...first,id:'second',title:'Second conversation'};
function bridge(overrides:Record<string,unknown>={}){
  const drafts=new Map<string,{revision:number;content:Record<string,unknown>}>();
  const values:Record<string,unknown>={bootstrap:{settings:{...DEFAULT_SETTINGS,model:'synthetic-model'},projects:[],chats:[],runs:[],capabilities:{}},models:{models:[{name:'synthetic-model'}]},projects:{projects:[]},chats:{chats:[]},runs:{runs:[]},spaces:{spaces:[]},show:{},host_status:{visible:true,visibility_seq:0},ui_layout_get:DEFAULT_LAYOUT,ui_layout_save:(data:Record<string,unknown>)=>data.layout,settings:(data:Record<string,unknown>)=>({...DEFAULT_SETTINGS,model:'synthetic-model',...data}),command:{message:'Synthetic request accepted.'},start_chat:{message:'Synthetic chat accepted.'},get_chat:(data:Record<string,unknown>)=>({...first,...(data.id===second.id?second:{}),messages:[]}),...overrides};
  const call=vi.fn(async(action:string,data:Record<string,unknown>={})=>{
    const key=JSON.stringify([data.scope_kind,data.project_id??null,data.chat_id??null]);
    if(action==='draft_get' && !(action in overrides))return drafts.get(key)||{revision:0,content:{}};
    if(action==='draft_save'){const row={revision:Number(data.expected_revision)+1,content:data.content as Record<string,unknown>};drafts.set(key,row);return row;}
    if(action==='draft_clear'){const row={revision:Number(data.expected_revision)+1,content:{}};drafts.set(key,row);return row;}
    const response=values[action];if(response===undefined)return {};return typeof response==='function'?response(data):response;
  });window.pywebview={api:{call,mode:async()=>({ok:true})}};return {call,drafts};
}
async function start(){render(<App/>);await act(async()=>window.dispatchEvent(new Event('pywebviewready')));await screen.findByText('What are we building?');}
const prompt=()=>screen.getByRole('textbox',{name:'Message Forge'}) as HTMLTextAreaElement;
describe('composer workflow modes',()=>{
  it('consumes only workflow prefixes and preserves explicit action precedence',()=>{
    expect(consumeWorkflowPrefix('/goal Build the page')).toEqual({mode:'goal',text:'Build the page'});expect(consumeWorkflowPrefix('/plan ')).toEqual({mode:'plan',text:''});expect(consumeWorkflowPrefix('/status ')).toBeNull();expect(requestMode('/status','goal')).toBeNull();expect(requestMode('Build the page','goal')).toBe('goal');
    expect(requestMode('/src/file.py inspect this','do')).toBe('do');expect(requestMode('/unknown literal','goal')).toBe('goal');expect(requestMode('/unknown',null)).toBeNull();
  });
  it('selects an empty /goal with Enter, then submits ordinary editable text and clears the accepted mode',async()=>{
    const {call}=bridge();await start();fireEvent.change(prompt(),{target:{value:'/goal'}});fireEvent.keyDown(prompt(),{key:'Enter'});expect(prompt().value).toBe('');expect(screen.getByText('Goal mode')).toBeTruthy();expect(call.mock.calls.some(([action])=>action==='command')).toBe(false);
    fireEvent.change(prompt(),{target:{value:'Build a clear settings screen'}});fireEvent.keyDown(prompt(),{key:'Enter'});await waitFor(()=>expect(call).toHaveBeenCalledWith('command',expect.objectContaining({text:'Build a clear settings screen',command_mode:'goal',permission_profile:'always_ask'})));await waitFor(()=>expect(screen.queryByText('Goal mode')).toBeNull());expect(prompt().value).toBe('');
  });
  it('selects Plan by click and removes the chip without changing the prompt',async()=>{
    bridge();await start();fireEvent.change(prompt(),{target:{value:'/pl'}});expect(prompt().value).toBe('/pl');expect(screen.queryByRole('listbox',{name:'Slash commands'})).toBeTruthy();await userEvent.click(screen.getByRole('option',{name:'/plan · Inspect and prepare a plan'}));expect(prompt().value).toBe('');expect(document.activeElement).toBe(prompt());fireEvent.change(prompt(),{target:{value:'Investigate the parser'}});await userEvent.click(screen.getByRole('button',{name:'Remove Plan mode'}));expect(prompt().value).toBe('Investigate the parser');expect(screen.queryByText('Plan mode')).toBeNull();expect(document.activeElement).toBe(prompt());
  });
  it('keeps failed workflow text and mode, and sends pasted /do as a clean task',async()=>{
    const {call}=bridge({command:{error:'Synthetic submission failure'}});await start();fireEvent.change(prompt(),{target:{value:'/do Read the project'}});expect(prompt().value).toBe('Read the project');fireEvent.keyDown(prompt(),{key:'Enter'});await screen.findByText('Synthetic submission failure');expect(prompt().value).toBe('Read the project');expect(screen.getByText('Do mode')).toBeTruthy();expect(call).toHaveBeenCalledWith('command',expect.objectContaining({text:'Read the project',command_mode:'do'}));
  });
  it('does not clear a newly edited mode when an earlier workflow submission completes',async()=>{
    const result=deferred();bridge({command:()=>result.promise});await start();fireEvent.change(prompt(),{target:{value:'/goal Build the page'}});fireEvent.keyDown(prompt(),{key:'Enter'});
    fireEvent.change(prompt(),{target:{value:'/plan Inspect the renderer'}});await act(async()=>result.resolve({message:'Accepted original workflow.'}));expect(prompt().value).toBe('Inspect the renderer');expect(screen.getByText('Plan mode')).toBeTruthy();
  });
  it('keeps the next workflow selected when an explicit action overrides it',async()=>{
    const {call}=bridge();await start();fireEvent.change(prompt(),{target:{value:'/goal '}});fireEvent.change(prompt(),{target:{value:'/status'}});fireEvent.keyDown(prompt(),{key:'Enter'});await waitFor(()=>expect(call).toHaveBeenCalledWith('command',expect.objectContaining({text:'/status',command_mode:'goal'})));await waitFor(()=>expect(prompt().value).toBe(''));expect(screen.getByText('Goal mode')).toBeTruthy();
  });
  it('submits a slash-prefixed file path in the selected workflow and consumes that mode after ACK',async()=>{
    const {call}=bridge();await start();fireEvent.change(prompt(),{target:{value:'/do '}});fireEvent.change(prompt(),{target:{value:'/src/file.py inspect this'}});fireEvent.keyDown(prompt(),{key:'Enter'});await waitFor(()=>expect(call).toHaveBeenCalledWith('command',expect.objectContaining({text:'/src/file.py inspect this',command_mode:'do'})));await waitFor(()=>expect(screen.queryByText('Do mode')).toBeNull());
  });
  it('persists mode-only drafts and restores each chat mode in its own scope',async()=>{
    const {call}=bridge({bootstrap:{settings:{...DEFAULT_SETTINGS,model:'synthetic-model'},projects:[],chats:[first,second],runs:[],capabilities:{}},chats:{chats:[first,second]},draft_get:(data:Record<string,unknown>)=>({revision:2,content:data.chat_id===first.id?{text:'First draft',command_mode:'plan'}:data.chat_id===second.id?{text:'Second draft',command_mode:'do'}:{}})});render(<App/>);await screen.findByText('Plan mode');expect(prompt().value).toBe('First draft');await userEvent.click(screen.getByRole('button',{name:'Second conversation'}));await screen.findByText('Do mode');expect(prompt().value).toBe('Second draft');
    await userEvent.click(screen.getByRole('button',{name:/New chat/}));fireEvent.change(prompt(),{target:{value:'/goal '}});await waitFor(()=>expect(call).toHaveBeenCalledWith('draft_save',expect.objectContaining({scope_kind:'new_chat',content:expect.objectContaining({text:'',command_mode:'goal'})})));
  });
});
