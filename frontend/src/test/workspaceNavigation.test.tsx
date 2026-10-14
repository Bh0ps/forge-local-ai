import { useState } from 'react';
import { Folder } from 'lucide-react';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { groupWorkspaceTools } from '../workspaceNavigation';
import { WorkspaceSidebar } from '../workspaceSidebar';
import { WorkspacePanel } from '../workspacePanel';
import type { Page } from '../types';

afterEach(()=>{cleanup();vi.restoreAllMocks();});
const items = [
  ['builder','Builder'],['projects','Projects'],['spaces','Spaces'],['scheduled','Scheduled'],
  ['plugins','Library'],['agents','Agents'],['usage','Usage'],['memory','Memory'],
].map(([page,label])=>({page:page as Page,label,icon:Folder}));
const project={id:'studio',name:'Studio',path:'/synthetic/studio',appearance:{icon:'code' as const,accent:'sage' as const}};
const chat={id:'chat',title:'Keep this conversation',model:'fixture',project_id:'studio'};
function sidebar(extra:Partial<React.ComponentProps<typeof WorkspaceSidebar>>={}) {
  const navigate=vi.fn(),select=vi.fn(),call=vi.fn(async()=>({}));window.pywebview={api:{call}};
  const props={page:'chat' as Page,projects:[project],chats:[chat],projectId:'studio',chatId:'chat',search:'',activeChatIds:[],navItems:items,
    onPage:navigate,onSearch:vi.fn(),onNew:vi.fn(),onCollapse:vi.fn(),onConnect:vi.fn(),onProject:vi.fn(),onChat:select,
    onChatChanged:vi.fn(async()=>{}),onProjectRemoved:vi.fn(async()=>{}),accent:'blue' as const,...extra};
  const view=render(<WorkspaceSidebar {...props}/>);return {...view,props,navigate,select,call};
}

describe('existing tools grouped without losing supplied destinations',()=>{
  it('keeps canonical tools once in original order while Projects stays in the primary tree',()=>{
    const groups=groupWorkspaceTools(items);
    expect(groups.map(group=>group.label)).toEqual(['Work','Manage','Inspect']);
    expect(groups.flatMap(group=>group.items)).toEqual(items.filter(item=>item.page!=='projects'));
    expect(items.map(item=>item.label)).toContain('Projects');
  });
  it('preserves unknown and duplicate supplied rows in place instead of moving them to a fallback tail',()=>{
    const rows=[{page:'usage'},{page:'custom'},{page:'builder'},{page:'constructor'},{page:'memory'},{page:'custom'}];
    const groups=groupWorkspaceTools(rows);
    expect(groups.flatMap(group=>group.items)).toEqual(rows);
    expect(groups[1].label).toBe('Other tools');expect(groups[3].label).toBe('Other tools');
    expect(groups.flatMap(group=>group.items).filter(item=>item.page==='custom')).toHaveLength(2);
  });
  it('keeps the project and chat controls above the footer tools with actual selected identities',async()=>{
    const {select,call}=sidebar();const tree=document.querySelector('.project-tree')!,tools=screen.getByRole('button',{name:'Tools'});
    expect(tree.compareDocumentPosition(tools)&Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(document.querySelector('.sidebar')?.getAttribute('data-accent')).toBe('blue');
    expect(document.querySelector('.project-group')?.getAttribute('data-accent')).toBe('sage');
    expect(screen.getByRole('button',{name:'Keep this conversation'}).classList.contains('active')).toBe(true);
    await userEvent.click(screen.getByRole('button',{name:'Keep this conversation'}));expect(select).toHaveBeenCalledWith('chat');expect(call).not.toHaveBeenCalled();
  });
  it('traverses only menu actions across group boundaries and calls the original destination once',async()=>{
    const {navigate,call}=sidebar();const trigger=screen.getByRole('button',{name:'Tools'});
    await userEvent.click(trigger);const menu=screen.getByRole('menu',{name:'Workspace tools'});
    expect(within(menu).getAllByRole('group').map(group=>group.getAttribute('aria-label'))).toEqual(['Work','Manage','Inspect']);
    expect(within(menu).getAllByRole('menuitem').map(button=>button.textContent)).toEqual(['Builder','Spaces','Scheduled','Library','Agents','Usage','Memory']);
    expect(document.activeElement).toBe(within(menu).getByRole('menuitem',{name:'Builder'}));
    await userEvent.keyboard('{ArrowUp}');expect(document.activeElement).toBe(within(menu).getByRole('menuitem',{name:'Memory'}));
    await userEvent.keyboard('{ArrowDown}{ArrowDown}{Enter}');expect(navigate).toHaveBeenCalledExactlyOnceWith('spaces');
    expect(document.activeElement).toBe(trigger);expect(screen.queryByRole('menu')).toBeNull();expect(call).not.toHaveBeenCalled();
  });
  it('retains unfamiliar supplied destinations and current indication in the real menu',async()=>{
    const custom={page:'chat' as Page,label:'Conversation tools',icon:Folder};const {navigate}=sidebar({navItems:[items[0],custom,items[6]]});
    await userEvent.click(screen.getByRole('button',{name:'Tools'}));const button=screen.getByRole('menuitem',{name:'Conversation tools'});
    expect(button.getAttribute('aria-current')).toBe('page');await userEvent.click(button);expect(navigate).toHaveBeenCalledExactlyOnceWith('chat');
  });
  it('does not steal menu focus or navigate when supplied counts and current page rerender',async()=>{
    const view=sidebar();await userEvent.click(screen.getByRole('button',{name:'Tools'}));const memory=screen.getByRole('menuitem',{name:'Memory'});memory.focus();
    view.rerender(<WorkspaceSidebar {...view.props} page="usage" scheduledCount={3} navItems={items.map(item=>({...item}))}/>);
    expect(document.activeElement).toBe(memory);expect(screen.getByRole('menuitem',{name:'Usage'}).getAttribute('aria-current')).toBe('page');
    await userEvent.keyboard('{Escape}');expect(document.activeElement).toBe(screen.getByRole('button',{name:'Tools'}));expect(view.navigate).not.toHaveBeenCalled();
  });
  it('keeps the focused destination when supplied rows change order within its group',async()=>{
    const view=sidebar();await userEvent.click(screen.getByRole('button',{name:'Tools'}));const spaces=screen.getByRole('menuitem',{name:'Spaces'});spaces.focus();
    const reordered=[items[2],items[0],items[1],...items.slice(3)];view.rerender(<WorkspaceSidebar {...view.props} navItems={reordered}/>);
    expect(document.activeElement).toBe(spaces);expect(screen.getByRole('menuitem',{name:'Spaces'})).toBe(spaces);
    await userEvent.keyboard('{Enter}');expect(view.navigate).toHaveBeenCalledExactlyOnceWith('spaces');
  });
  it('keeps the same pending rename owner through a harmless sidebar rerender',async()=>{
    let resolve!:(value:unknown)=>void;const pending=new Promise(done=>{resolve=done;});const call=vi.fn(async()=>pending);window.pywebview={api:{call}};
    const view=sidebar();window.pywebview={api:{call}};
    await userEvent.click(screen.getByRole('button',{name:'Chat actions for Keep this conversation'}));await userEvent.click(screen.getByRole('menuitem',{name:'Rename'}));
    const dialog=screen.getByRole('dialog'),field=within(dialog).getByRole('textbox',{name:'Conversation title'});
    await userEvent.clear(field);await userEvent.type(field,'Retained rename');await userEvent.click(within(dialog).getByRole('button',{name:'Save'}));
    await waitFor(()=>expect(call).toHaveBeenCalledOnce());view.rerender(<WorkspaceSidebar {...view.props} scheduledCount={4}/>);
    expect(screen.getByRole('dialog')).toBe(dialog);expect(screen.getByRole('textbox',{name:'Conversation title'})).toBe(field);expect(field).toHaveProperty('value','Retained rename');
    expect(call).toHaveBeenCalledWith('rename_chat',{id:'chat',title:'Retained rename'});await act(async()=>resolve({ok:true}));
    await waitFor(()=>expect(screen.queryByRole('dialog')).toBeNull());expect(call).toHaveBeenCalledOnce();
  });
});

describe('compact detail tabs keep their actual button ownership',()=>{
  function Pane(){const [tab,setTab]=useState('activity');return <WorkspacePanel tab={tab} select={setTab} close={()=>{}} width={320} resize={()=>{}}><p>{tab} evidence</p></WorkspacePanel>;}
  it('shows visual descriptions without navigating or replacing the true tab names',async()=>{
    render(<Pane/>);const tabs=screen.getAllByRole('tab');expect(tabs.map(tab=>tab.getAttribute('aria-label'))).toEqual(['Context','Browser','Activity','Goals','Files']);
    const browser=screen.getByRole('tab',{name:'Browser'});await userEvent.hover(browser);
    const label=document.querySelector('.control-label')!;expect(label.textContent).toBe('Browser · Browse alongside your agent.');expect(label.getAttribute('aria-hidden')).toBe('true');
    expect(screen.getByRole('tab',{name:'Activity'}).getAttribute('aria-selected')).toBe('true');
    fireEvent.click(label);expect(screen.getByRole('tabpanel',{name:'Activity'})).toBeTruthy();
    browser.focus();await userEvent.keyboard('{Escape}');expect(document.querySelector('.control-label')).toBeNull();expect(document.activeElement).toBe(browser);
  });
  it('keeps tab refs, single roving stop and keyboard selection with a focused visual label',async()=>{
    render(<Pane/>);const activity=screen.getByRole('tab',{name:'Activity'});activity.focus();
    await userEvent.keyboard('{Home}');expect(document.activeElement).toBe(screen.getByRole('tab',{name:'Context'}));
    expect(screen.getByRole('tabpanel',{name:'Context'})).toBeTruthy();expect(screen.getAllByRole('tab').filter(tab=>tab.tabIndex===0)).toHaveLength(1);
    await userEvent.keyboard('{End}{ArrowRight}');expect(document.activeElement).toBe(screen.getByRole('tab',{name:'Context'}));
  });
});
