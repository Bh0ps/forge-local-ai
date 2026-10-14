import { useState } from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import App from '../App';
import { IconButton } from '../components';
import { ContextControl } from '../contextControl';
import { DEFAULT_SETTINGS } from '../types';
import { DEFAULT_LAYOUT } from '../workspaceLayout';

const label = () => document.querySelector<HTMLSpanElement>('.control-label');
const chat = {id:'control-chat',title:'Control fixture',project_id:null,model:'fixture',messages:[{id:1,role:'user',content:'Synthetic question'},{id:2,role:'assistant',content:'Synthetic saved answer'}]};
function bridge() {
  const responses: Record<string, unknown> = {
    bootstrap:{settings:{...DEFAULT_SETTINGS,model:'fixture'},projects:[],chats:[chat],runs:[],capabilities:{}},
    get_chat:chat,projects:{projects:[]},chats:{chats:[chat]},models:{models:[{name:'fixture'}]},runs:{runs:[]},
    schedules:{schedules:[]},spaces:{spaces:[]},pending_questions:{questions:[]},setup_status:{first_run:false},
    draft_get:{revision:0,content:{text:'Keep this draft'}},draft_save:({content}:Record<string,unknown>)=>({revision:1,content}),
    ui_layout_get:DEFAULT_LAYOUT,ui_layout_save:({layout}:Record<string,unknown>)=>layout,show:{capabilities:[],model_info:{}},
    host_status:{visible:true,visibility_seq:0},
  };
  const call = vi.fn(async (action:string, data:Record<string,unknown>={}) => {
    if (!(action in responses)) throw Error('Unexpected synthetic action');
    const result=responses[action];return typeof result==='function'?result(data):result;
  });
  window.pywebview={api:{call,copy:vi.fn(async()=>({ok:true})),mode:vi.fn(async()=>({ok:true}))}};
  return call;
}

describe('everyday icon control labels', () => {
  it('keeps unopted controls unchanged and activates the real button only once', async () => {
    const action=vi.fn();render(<IconButton label="Plain action" onClick={action}><span aria-hidden="true">+</span></IconButton>);
    const button=screen.getByRole('button',{name:'Plain action'});
    fireEvent.mouseEnter(button);act(()=>button.focus());fireEvent.keyDown(button,{key:'Escape'});
    expect(label()).toBeNull();expect(action).not.toHaveBeenCalled();expect(button.title).toBe('Plain action');
    await userEvent.keyboard('{Enter}');expect(action).toHaveBeenCalledOnce();
  });

  it('shows the same label for hover and keyboard focus without changing the accessible name', () => {
    const action=vi.fn();render(<IconButton label="Attach files" showLabel onClick={action}><span aria-hidden="true">+</span></IconButton>);
    const button=screen.getByRole('button',{name:'Attach files'});
    fireEvent.mouseEnter(button);expect(label()?.textContent).toBe('Attach files');
    expect(label()?.getAttribute('aria-hidden')).toBe('true');expect(button.title).toBe('');
    fireEvent.mouseLeave(button);expect(label()).toBeNull();act(()=>button.focus());
    expect(label()?.textContent).toBe('Attach files');expect(document.activeElement).toBe(button);
    expect(screen.getByRole('button',{name:'Attach files'})).toBe(button);expect(action).not.toHaveBeenCalled();
  });

  it('lets Escape dismiss a focused label without moving focus or invoking another action', async () => {
    const action=vi.fn(),escape=vi.fn();window.addEventListener('keydown',escape);
    render(<IconButton label="Send message" labelText="Send message (Enter)" showLabel onClick={action}><span aria-hidden="true">↑</span></IconButton>);
    const button=screen.getByRole('button',{name:'Send message'});act(()=>button.focus());
    expect(label()?.textContent).toBe('Send message (Enter)');await userEvent.keyboard('{Escape}');
    expect(label()).toBeNull();expect(document.activeElement).toBe(button);expect(action).not.toHaveBeenCalled();expect(escape).not.toHaveBeenCalled();
    await userEvent.keyboard('{Enter}');expect(action).toHaveBeenCalledOnce();window.removeEventListener('keydown',escape);
  });

  it('keeps a focused label while the pointer leaves and does not activate through the visual label', () => {
    const action=vi.fn();render(<><IconButton label="Helpful action" showLabel onClick={action}><span aria-hidden="true">+</span></IconButton><button>Other control</button></>);
    const button=screen.getByRole('button',{name:'Helpful action'});act(()=>button.focus());fireEvent.mouseEnter(button);fireEvent.mouseLeave(button);
    const hint=label()!;expect(hint).toBeTruthy();fireEvent.pointerDown(hint);fireEvent.click(hint);
    expect(action).not.toHaveBeenCalled();expect(document.activeElement).toBe(button);
    act(()=>screen.getByRole('button',{name:'Other control'}).focus());expect(label()).toBeNull();
  });

  it('retains pressed and disabled semantics and retires a label when the control becomes disabled', () => {
    const action=vi.fn(),view=render(<IconButton label="Context action" showLabel active onClick={action}>+</IconButton>);
    const button=screen.getByRole('button',{name:'Context action'});act(()=>button.focus());expect(button.getAttribute('aria-pressed')).toBe('true');expect(label()).toBeTruthy();
    view.rerender(<IconButton label="Context action" showLabel disabled active onClick={action}>+</IconButton>);
    expect(label()).toBeNull();fireEvent.mouseEnter(button);fireEvent.click(button);expect(action).not.toHaveBeenCalled();expect(button).toHaveProperty('disabled',true);
  });

  it('dismisses on hidden host, document, viewport changes and blur without automatically replaying', () => {
    render(<IconButton label="Visible action" showLabel>+</IconButton>);const button=screen.getByRole('button',{name:'Visible action'});
    act(()=>button.focus());fireEvent(window,new CustomEvent('forge:visibility',{detail:{visible:false}}));expect(label()).toBeNull();
    fireEvent(window,new CustomEvent('forge:visibility',{detail:{visible:true}}));expect(label()).toBeNull();
    act(()=>button.blur());act(()=>button.focus());expect(label()).toBeTruthy();fireEvent(window,new Event('resize'));expect(label()).toBeNull();
    act(()=>button.blur());act(()=>button.focus());const state=vi.spyOn(document,'visibilityState','get').mockReturnValue('hidden');fireEvent(document,new Event('visibilitychange'));expect(label()).toBeNull();
    state.mockReturnValue('visible');fireEvent(document,new Event('visibilitychange'));expect(label()).toBeNull();
    act(()=>button.blur());act(()=>button.focus());fireEvent(window,new Event('blur'));expect(label()).toBeNull();
  });
});

describe('composer foundation preserves control ownership', () => {
  it('does not make API calls from labels and keeps grouped controls and the current draft', async () => {
    const call=bridge();render(<App/>);await act(async()=>window.dispatchEvent(new Event('pywebviewready')));await screen.findByText('Synthetic saved answer');
    const prompt=screen.getByRole('textbox',{name:'Message Forge'});await waitFor(()=>expect(prompt).toHaveProperty('value','Keep this draft'));
    expect(within(screen.getByRole('group',{name:'Attachments'})).getByRole('button',{name:'Attach files'})).toBeTruthy();
    expect(within(screen.getByRole('group',{name:'Model and permissions'})).getByRole('button',{name:'Ask'})).toBeTruthy();
    const calls=call.mock.calls.length;const attach=screen.getByRole('button',{name:'Attach files'});
    fireEvent.mouseEnter(attach);act(()=>attach.focus());fireEvent.keyDown(attach,{key:'Escape'});act(()=>prompt.focus());
    const notification=screen.getByRole('button',{name:'Notifications'});act(()=>notification.focus());expect(label()?.textContent).toBe('Notifications');
    fireEvent.keyDown(notification,{key:'Escape'});act(()=>prompt.focus());expect(call.mock.calls.length).toBe(calls);
    expect(prompt).toHaveProperty('value','Keep this draft');expect(screen.getByRole('button',{name:'Send message'})).toBeTruthy();
  });

  it('preserves the context popup ref, settings and Escape focus-return path', async () => {
    const call=bridge(),change=vi.fn(async()=>{});
    function Control(){const [open,setOpen]=useState(false);return <ContextControl settings={{...DEFAULT_SETTINGS,model:'fixture'}} onChange={change} open={open} setOpen={setOpen}/>;}
    render(<Control/>);await waitFor(()=>expect(call.mock.calls.some(([action])=>action==='show')).toBe(true));
    const trigger=screen.getByRole('button',{name:/^Context controls/});act(()=>trigger.focus());expect(label()).toBeTruthy();
    const before=call.mock.calls.length;await userEvent.keyboard('{Escape}');expect(label()).toBeNull();expect(document.activeElement).toBe(trigger);
    await userEvent.keyboard('{Enter}');expect(screen.getByRole('dialog',{name:'Context window controls'})).toBeTruthy();expect(label()).toBeNull();
    await userEvent.keyboard('{Escape}');expect(screen.queryByRole('dialog')).toBeNull();expect(document.activeElement).toBe(trigger);
    expect(change).not.toHaveBeenCalled();expect(call.mock.calls.length).toBe(before);
  });
});
