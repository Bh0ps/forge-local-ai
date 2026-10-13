import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { useState } from 'react';
import { expect, it, vi } from 'vitest';
import { usePersistentDraft, type DraftContent } from '../persistentDraft';

it.each(['text','mode'])('keeps a deliberately cleared %s after a delayed saved-draft reply', async(kind)=>{
  let reply!:(value:unknown)=>void;
  const call=vi.fn(async(action:string,data:Record<string,unknown>)=>{
    if(action==='draft_get')return new Promise(resolve=>{reply=resolve;});
    if(action==='draft_save')return {revision:2,content:data.content,scope_available:true};
    return {};
  });
  window.pywebview={api:{call}};
  function Composer(){
    const [content,setContent]=useState<DraftContent>({text:'',command_mode:null});
    usePersistentDraft({scope_kind:'new_chat',project_id:`restore-${kind}`},content,setContent);
    return <><input aria-label="Intent draft" value={content.text||''} onChange={event=>setContent(previous=>({...previous,text:event.target.value}))}/>
      <button onClick={()=>setContent(previous=>({...previous,command_mode:'goal'}))}>Choose goal</button>
      <button onClick={()=>setContent(previous=>({...previous,command_mode:null}))}>Remove mode</button>
      <output>{content.command_mode||'No mode'}</output></>;
  }
  const view=render(<Composer/>);
  try {
    await waitFor(()=>expect(reply).toBeTruthy());
    if(kind==='text'){
      fireEvent.change(screen.getByRole('textbox'),{target:{value:'New local intent'}});
      fireEvent.change(screen.getByRole('textbox'),{target:{value:''}});
    } else {
      fireEvent.click(screen.getByRole('button',{name:'Choose goal'}));
      fireEvent.click(screen.getByRole('button',{name:'Remove mode'}));
    }
    await act(async()=>reply({revision:1,content:{text:'Old saved task',command_mode:'plan'},scope_available:true}));
    expect((screen.getByRole('textbox') as HTMLInputElement).value).toBe('');
    expect(screen.getByText('No mode')).toBeTruthy();
    await waitFor(()=>expect(call).toHaveBeenCalledWith('draft_save',expect.objectContaining({expected_revision:1,content:{text:'',command_mode:null}})));
  } finally {view.unmount();delete window.pywebview;}
});
