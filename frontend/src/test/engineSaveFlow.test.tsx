import { act,fireEvent,render,screen,waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe,expect,it,vi } from 'vitest';
import App from '../App';
import { EngineEditor } from '../engineEditor';
import { SettingsPage } from '../settings';
import * as loading from '../settingsLoader';
import { DEFAULT_SETTINGS,type Provider } from '../types';
import { DEFAULT_LAYOUT } from '../workspaceLayout';

const provider:Provider={id:'',name:'Synthetic connection',type:'openai',base_url:'http://127.0.0.1:18080/v1'};
const marker='synthetic-private-error-marker';
function deferred(){let resolve!:(value:unknown)=>void,reject!:(error:Error)=>void;const promise=new Promise<unknown>((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};}
function bridge(save:(data:Record<string,unknown>)=>unknown){const call=vi.fn(async(action:string,data:Record<string,unknown>={})=>action==='provider_presets'?{presets:[]}:action==='provider_save'?save(data):{});window.pywebview={api:{call}};return call;}
function editor(props:Partial<{provider:Provider;close:()=>void;saved:()=>Promise<void>}>= {}){const saved=vi.fn(async()=>{}),close=vi.fn();const full={provider,close,saved,...props};return {...render(<EngineEditor {...full}/>),props:full,saved,close};}
const saveButton=()=>screen.getByRole('button',{name:'Save engine'});

describe('explicit connection saves',()=>{
  it('keeps one creation identity across a lost acknowledgement and a manual correction/retry',async()=>{
    const rows=new Map<string,unknown>();let first=true;
    const call=bridge(data=>{rows.set(String(data.id),data);if(first){first=false;throw new Error(marker);}return {id:data.id};});
    const {saved}=editor();const key=screen.getByLabelText('API key (optional)') as HTMLInputElement;
    await userEvent.type(key,'synthetic-only-key');await userEvent.click(saveButton());
    expect((await screen.findByRole('alert')).textContent).toContain('Could not confirm this save');
    expect(screen.getByRole('alert').textContent).not.toContain(marker);expect(key.value).toBe('synthetic-only-key');
    expect(call.mock.calls.filter(([action])=>action==='provider_save')).toHaveLength(1);
    fireEvent.change(screen.getByLabelText('Name'),{target:{value:'Corrected connection'}});await userEvent.click(saveButton());
    await waitFor(()=>expect(saved).toHaveBeenCalledOnce());const calls=call.mock.calls.filter(([action])=>action==='provider_save');
    expect(calls).toHaveLength(2);expect(calls[0]![1]!.id).toMatch(/^engine-[a-f0-9]{32}$/);expect(calls[1]![1]!.id).toBe(calls[0]![1]!.id);expect(rows.size).toBe(1);expect(key.value).toBe('');
  });
  it.each([undefined,'other-engine',17])('refuses an acknowledgement with an invalid identity (%s)',async id=>{
    const call=bridge(()=>({id}));const {saved}=editor();await userEvent.click(saveButton());
    expect((await screen.findByRole('alert')).textContent).toContain('The save may have completed');expect(saved).not.toHaveBeenCalled();
    expect(call.mock.calls.filter(([action])=>action==='provider_save')).toHaveLength(1);expect((saveButton() as HTMLButtonElement).disabled).toBe(false);
  });
  it('reports an actual rejected save without claiming success or automatically trying again',async()=>{
    const call=bridge(()=>{throw new Error(marker);});const {saved}=editor();await userEvent.click(saveButton());
    expect((await screen.findByRole('alert')).textContent).toContain('Check your connections');expect(screen.getByRole('alert').textContent).not.toContain(marker);
    expect(saved).not.toHaveBeenCalled();expect(call.mock.calls.filter(([action])=>action==='provider_save')).toHaveLength(1);
  });
  it('retains the existing profile ID and blocks overlapping explicit submissions',async()=>{
    const pending=deferred(),call=bridge(()=>pending.promise),{saved}=editor({provider:{...provider,id:'existing-profile'}});
    const form=saveButton().closest('form')!;fireEvent.submit(form);fireEvent.submit(form);
    expect(call.mock.calls.filter(([action])=>action==='provider_save')).toHaveLength(1);expect(call).toHaveBeenCalledWith('provider_save',expect.objectContaining({id:'existing-profile'}));
    await act(async()=>pending.resolve({id:'existing-profile'}));expect(saved).toHaveBeenCalledOnce();
  });
  it('replaces creation ownership and ignores the previous owner’s late save',async()=>{
    const pending=deferred();let first=true;const call=bridge(data=>{if(first){first=false;return pending.promise;}return {id:data.id};});
    const view=editor();await userEvent.type(screen.getByLabelText('API key (optional)'),'synthetic-only-key');fireEvent.submit(saveButton().closest('form')!);
    const oldId=call.mock.calls.find(([action])=>action==='provider_save')![1]!.id;
    view.rerender(<EngineEditor {...view.props} provider={{...provider,name:'Replacement owner'}}/>);
    expect((screen.getByLabelText('API key (optional)') as HTMLInputElement).value).toBe('');
    await act(async()=>pending.resolve({id:oldId}));expect(view.saved).not.toHaveBeenCalled();expect(screen.queryByRole('alert')).toBeNull();
    await userEvent.click(saveButton());await waitFor(()=>expect(view.saved).toHaveBeenCalledOnce());
    const calls=call.mock.calls.filter(([action])=>action==='provider_save');expect(calls[1]![1]!.id).not.toBe(oldId);expect(calls[1]![1]!.api_key).toBeUndefined();
  });
  it.each(['cancel','unmount'])('ignores a late save after %s',async mode=>{
    const pending=deferred(),call=bridge(()=>pending.promise),view=editor();await userEvent.type(screen.getByLabelText('API key (optional)'),'synthetic-only-key');fireEvent.submit(saveButton().closest('form')!);
    const id=call.mock.calls.find(([action])=>action==='provider_save')![1]!.id;
    if(mode==='cancel'){await userEvent.click(screen.getByRole('button',{name:'Cancel'}));expect((screen.getByLabelText('API key (optional)') as HTMLInputElement).value).toBe('');expect(view.close).toHaveBeenCalledOnce();}else view.unmount();
    await act(async()=>pending.resolve({id}));expect(view.saved).not.toHaveBeenCalled();expect(call.mock.calls.filter(([action])=>action==='provider_save')).toHaveLength(1);
  });
  it('clears the key after a confirmed save even if the refresh callback fails',async()=>{
    bridge(data=>({id:data.id}));editor({saved:async()=>{throw new Error(marker);}});const key=screen.getByLabelText('API key (optional)') as HTMLInputElement;await userEvent.type(key,'synthetic-only-key');await userEvent.click(saveButton());
    expect((await screen.findByRole('alert')).textContent).toContain('Your connection was saved');expect(screen.getByRole('alert').textContent).not.toContain(marker);expect(key.value).toBe('');
  });
});

async function actualApp(failure:'models'|'providers'){
  vi.spyOn(loading,'loadSettingsPage').mockResolvedValue({default:SettingsPage});
  const chat={id:'save-flow-chat',title:'Saved connection fixture',project_id:null,model:'fixture',messages:[{role:'assistant',content:'Retained connection conversation'}]};
  const drafts=new Map<string,{revision:number;content:unknown}>();drafts.set(chat.id,{revision:1,content:{text:'Keep this draft'}});let committed=false;
  const call=vi.fn(async(action:string,data:Record<string,unknown>={})=>{
    if(action==='bootstrap')return {settings:{...DEFAULT_SETTINGS,model:'fixture'},projects:[],chats:[chat],runs:[],capabilities:{}};
    if(action==='get_chat')return chat;
    if(action==='setup_status')return {first_run:false};
    if(action==='ui_layout_get')return DEFAULT_LAYOUT;
    if(action==='ui_layout_save')return data.layout;
    if(action==='draft_get')return drafts.get(String(data.chat_id))||{revision:0,content:{}};
    if(action==='draft_save'){const next={revision:Number(data.expected_revision)+1,content:data.content};drafts.set(String(data.chat_id),next);return next;}
    if(action==='provider_save'){committed=true;return {id:data.id};}
    if(action==='providers'){if(committed && failure==='providers')throw new Error('Synthetic connection-list refresh unavailable');return {providers:[]};}
    if(action==='models'){if(committed && failure==='models')throw new Error('Synthetic model refresh unavailable');return {models:[]};}
    if(action==='provider_presets')return {presets:[]};
    if(action==='pending_questions')return {questions:[]};
    if(['projects','chats','runs','schedules','spaces'].includes(action))return {[action]:action==='chats'?[chat]:[]};
    return {};
  });window.pywebview={api:{call}};render(<App/>);await act(async()=>window.dispatchEvent(new Event('pywebviewready')));await screen.findByText('Retained connection conversation');
  await waitFor(()=>expect((screen.getByRole('textbox',{name:'Message Forge'}) as HTMLTextAreaElement).value).toBe('Keep this draft'));
  await userEvent.click(screen.getByRole('button',{name:'Settings'}));await userEvent.click(await screen.findByRole('button',{name:'Models & engines'}));await userEvent.click(await screen.findByRole('button',{name:'Add local endpoint'}));
  fireEvent.change(screen.getByLabelText('Name'),{target:{value:'Synthetic connection'}});fireEvent.change(screen.getByLabelText('Base URL'),{target:{value:provider.base_url}});await userEvent.click(saveButton());return {call,chat,drafts};
}
describe('confirmed connection save in the actual workspace',()=>{
  it('keeps the post-save model refresh warning visible and preserves the conversation draft',async()=>{
    const fixture=await actualApp('models');await screen.findByText('Synthetic model refresh unavailable');expect(screen.queryByRole('dialog',{name:'Add local engine'})).toBeNull();
    expect(fixture.call.mock.calls.filter(([action])=>action==='provider_save')).toHaveLength(1);expect(screen.queryByText('Engine saved.')).toBeNull();
    await userEvent.click(screen.getByRole('button',{name:'Saved connection fixture'}));await waitFor(()=>expect((screen.getByRole('textbox',{name:'Message Forge'}) as HTMLTextAreaElement).value).toBe('Keep this draft'));expect(fixture.drafts.get(fixture.chat.id)?.content).toMatchObject({text:'Keep this draft'});
  });
  it('retains the provider-list inline error alongside the confirmed save notice',async()=>{
    const fixture=await actualApp('providers');await screen.findByText('Synthetic connection-list refresh unavailable');expect(screen.getByText('Engine saved.')).toBeTruthy();expect(fixture.call.mock.calls.filter(([action])=>action==='provider_save')).toHaveLength(1);
  });
});
