import { act, fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { AppearanceSettings, ProjectAppearanceFields } from '../appearance';
import { MessageView, Toggle } from '../components';
import { DEFAULT_SETTINGS, type Message } from '../types';
import type { ThinkingReadIntent } from '../thinkingDisclosure';

afterEach(()=>vi.useRealTimers());

describe('chat and appearance controls',()=>{
  it('associates each switch with its own supporting text and retains checked and disabled semantics',async()=>{
    const change=vi.fn(),view=render(<><Toggle label="Computer tools" description="Enable local desktop tools." checked onChange={change}/><Toggle label="Browser tools" description="Enable browser tools." checked={false} disabled onChange={change}/></>);
    const computer=screen.getByRole('switch',{name:'Computer tools'}),browser=screen.getByRole('switch',{name:'Browser tools'});
    const hint=computer.getAttribute('aria-describedby')!;
    expect(document.getElementById(hint)?.textContent).toBe('Enable local desktop tools.');
    expect(browser.getAttribute('aria-describedby')).not.toBe(hint);
    expect(browser.getAttribute('aria-checked')).toBe('false');expect(browser).toHaveProperty('disabled',true);
    await userEvent.click(computer);expect(change).toHaveBeenCalledWith(false);
    view.rerender(<Toggle label="Computer tools" checked onChange={change}/>);
    expect(computer.hasAttribute('aria-describedby')).toBe(false);expect(document.getElementById(hint)).toBeNull();
  });

  it('gives project icons discoverable hover and keyboard labels without changing selection on focus',async()=>{
    const change=vi.fn();render(<ProjectAppearanceFields value={{icon:'folder',accent:'sage'}} change={change}/>);
    const choices=screen.getByRole('group',{name:'Project icon'}),code=within(choices).getByRole('button',{name:'Code icon'});
    expect(within(choices).getByRole('button',{name:'Folder icon'}).getAttribute('aria-pressed')).toBe('true');
    act(()=>code.focus());expect(code.querySelector('.control-label')?.textContent).toBe('Code icon');expect(change).not.toHaveBeenCalled();
    await userEvent.keyboard('{Escape}');expect(code.querySelector('.control-label')).toBeNull();expect(document.activeElement).toBe(code);
    await userEvent.keyboard('{Enter}');expect(change).toHaveBeenCalledWith({icon:'code',accent:'sage'});
    const globe=within(choices).getByRole('button',{name:'Globe icon'});fireEvent.mouseEnter(globe);expect(globe.querySelector('.control-label')?.textContent).toBe('Globe icon');
  });

  it('keeps theme next to appearance and preserves a workspace-name draft while changing it',async()=>{
    const change=vi.fn(async()=>{});render(<AppearanceSettings settings={DEFAULT_SETTINGS} onChange={change}/>);
    const name=screen.getByRole('textbox',{name:'Workspace name'});await userEvent.clear(name);await userEvent.type(name,'Retained studio');
    await userEvent.selectOptions(screen.getByRole('combobox',{name:'Appearance'}),'dark');
    expect(change).toHaveBeenCalledWith({theme:'dark'});expect(name).toHaveProperty('value','Retained studio');
  });
});

describe('quiet message rendering',()=>{
  it('skips unchanged persisted rows while responding to a changed read owner, display preference, content and status',()=>{
    const content=vi.fn(()=>'Saved answer.');
    const message:Message={id:1,role:'assistant',content:'',thinking:'Saved thought.'};Object.defineProperty(message,'content',{get:content,enumerable:true});
    const view=render(<MessageView message={message}/>),reads=content.mock.calls.length;
    view.rerender(<MessageView message={message}/>);expect(content).toHaveBeenCalledTimes(reads);
    const intent:ThinkingReadIntent={key:'owned-thought',current:{open:true,following:false,scrollTop:15,latestAvailable:true}};
    view.rerender(<MessageView message={message} thinkingIntent={intent}/>);expect(content.mock.calls.length).toBeGreaterThan(reads);
    expect(document.querySelector('details')?.open).toBe(true);
    view.rerender(<MessageView message={message} thinkingIntent={intent} showThinking={false}/>);expect(document.querySelector('.thinking-disclosure')).toBeNull();
    view.rerender(<MessageView message={{...message,content:'Updated answer.',status:'Interrupted'}}/>);
    expect(screen.getByText('Updated answer.')).toBeTruthy();expect(screen.getByText('Interrupted').className).toBe('message-status');
    view.rerender(<MessageView message={message} streaming/>);expect(screen.queryByRole('group',{name:'Message actions'})).toBeNull();
  });

  it('retains the assistant author for screen readers and provides semantic copy feedback and failures',async()=>{
    const copy=vi.fn(async()=>({ok:true}));window.pywebview={api:{call:vi.fn(),copy}};
    render(<MessageView message={{role:'assistant',content:'A clear answer.',status:'Interrupted'}}/>);
    expect(screen.getByText('Forge').classList.contains('sr-only')).toBe(true);expect(screen.getByText('Interrupted').classList.contains('sr-only')).toBe(false);
    const actions=screen.getByRole('group',{name:'Message actions'});await userEvent.click(within(actions).getByRole('button',{name:'Copy response'}));
    expect(copy).toHaveBeenCalledWith('A clear answer.');expect(within(actions).getByRole('status').textContent).toBe('Response copied');expect(actions.getAttribute('data-feedback')).toBe('true');
    copy.mockRejectedValueOnce(Error('Clipboard unavailable'));await userEvent.click(within(actions).getByRole('button',{name:'Copy response'}));
    expect(screen.getByRole('alert').textContent).toContain('Could not copy.');expect(within(actions).getByRole('status').textContent).toBe('');expect(actions.getAttribute('data-feedback')).toBe('true');
  });

  it('clears message and code copy-feedback timers on unmount',async()=>{
    vi.useFakeTimers();window.pywebview={api:{call:vi.fn(),copy:vi.fn(async()=>({ok:true}))}};
    const view=render(<MessageView message={{role:'assistant',content:'```ts\nconst ready = true;\n```'}}/>);
    await act(async()=>{fireEvent.click(screen.getByRole('button',{name:'Copy code'}));fireEvent.click(screen.getByRole('button',{name:'Copy response'}));});
    expect(vi.getTimerCount()).toBe(2);view.unmount();expect(vi.getTimerCount()).toBe(0);
  });

  it('does not schedule feedback when a pending clipboard operation completes after unmount',async()=>{
    vi.useFakeTimers();let finish!:()=>void;const pending=new Promise<void>(resolve=>{finish=resolve;});
    window.pywebview={api:{call:vi.fn(),copy:vi.fn(()=>pending)}};
    const view=render(<MessageView message={{role:'assistant',content:'```ts\nconst ready = true;\n```'}}/>);
    fireEvent.click(screen.getByRole('button',{name:'Copy code'}));fireEvent.click(screen.getByRole('button',{name:'Copy response'}));view.unmount();
    await act(async()=>finish());expect(vi.getTimerCount()).toBe(0);
  });
});
