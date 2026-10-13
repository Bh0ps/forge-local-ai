import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { expect, it, vi } from 'vitest';
import App from '../App';
import { DEFAULT_SETTINGS } from '../types';

it.each(['rejected','mismatched'])('propagates a %s context save through App and retries only after an acknowledged write', async(kind)=>{
  let refuse=true;
  const call=vi.fn(async(action:string,data:Record<string,unknown>={})=>{
    if(action==='bootstrap')return {settings:{...DEFAULT_SETTINGS,model:'fixture'},projects:[],chats:[],runs:[],capabilities:{}};
    if(action==='models')return {models:[{name:'fixture'}]};
    if(action==='show')return {capabilities:['tools'],model_info:{'fixture.context_length':131072}};
    if(action==='providers')return {providers:[{id:'ollama',name:'Ollama',kind:'ollama'}]};
    if(action==='draft_get')return {revision:0,content:{},scope_available:true};
    if(action==='settings')return refuse ? kind==='rejected' ? {error:'Synthetic refused settings write'} : {...DEFAULT_SETTINGS,model:'fixture',context:32768} : {...DEFAULT_SETTINGS,model:'fixture',...data};
    return {};
  });
  window.pywebview={api:{call}};
  const view=render(<App/>);
  try {
    await act(async()=>window.dispatchEvent(new Event('pywebviewready')));
    await screen.findByText('What are we building?');
    await userEvent.click(screen.getByRole('button',{name:'Settings'}));
    await userEvent.click(await screen.findByRole('button',{name:'Low 2K'}));
    expect(await screen.findByText('Context could not be saved.')).toBeTruthy();
    expect(call.mock.calls.filter(([action])=>action==='settings')).toHaveLength(1);
    expect(call).toHaveBeenCalledWith('settings',{context:2048,adaptive_context_locked:true});
    refuse=false;
    await userEvent.click(screen.getByRole('button',{name:'Retry context save'}));
    await waitFor(()=>expect(screen.queryByText('Context could not be saved.')).toBeNull());
    expect(call.mock.calls.filter(([action])=>action==='settings')).toHaveLength(2);
  } finally {view.unmount();delete window.pywebview;}
});
