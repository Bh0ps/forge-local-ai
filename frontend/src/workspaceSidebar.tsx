import { useEffect, useRef, useState } from 'react';
import { Archive, ChevronDown, ChevronRight, Folder, FolderPlus, PanelLeftClose, Plus, Search, Settings2, Wrench } from 'lucide-react';
import { IconButton } from './components';
import { ChatRow } from './chatActions';
import { ProjectActions } from './projectActions';
import type { ChatChange } from './chatActions';
import { ProjectIcon } from './appearance';
import { ResizeHandle } from './workspaceLayout';
import { groupWorkspaceTools } from './workspaceNavigation';
import type { Accent, Chat, Page, Project } from './types';
interface SidebarProps {
  page: Page; projects: Project[]; chats: Chat[]; projectId: string | null; chatId: string | null;
  search: string; onSearch: (value: string) => void; activeChatIds: string[]; scheduledCount?: number;
  navItems: { page: Page; label: string; icon: typeof Folder }[];
  onPage: (page: Page) => void; onNew: () => void; onCollapse: () => void; onConnect: () => void;
  onProject: (id: string | null) => void; onChat: (id: string) => void;
  onChatChanged: (id: string, change: ChatChange) => Promise<void>;
  onProjectRemoved: (id: string) => Promise<void>;
  workspaceName?:string; accent?:Accent; width?:number; onWidth?:(width:number)=>void;
  collapsedProjectIds?:string[]; onCollapsedProjects?:(ids:string[])=>void;
  settingsOpen?: boolean;
}
export function WorkspaceSidebar(props: SidebarProps) {
  const [archivesOpen, setArchivesOpen] = useState(false);
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});
  const [toolsOpen,setToolsOpen]=useState(false), toolsRoot=useRef<HTMLDivElement>(null), toolsTrigger=useRef<HTMLButtonElement>(null), toolsMenu=useRef<HTMLDivElement>(null);
  useEffect(()=>{if(!toolsOpen)return;toolsMenu.current?.querySelector<HTMLButtonElement>('button')?.focus();const pointer=(event:MouseEvent)=>{if(!toolsRoot.current?.contains(event.target as Node))setToolsOpen(false);};const keys=(event:KeyboardEvent)=>{if(event.key==='Escape'){event.preventDefault();setToolsOpen(false);toolsTrigger.current?.focus();}else if(['ArrowDown','ArrowUp'].includes(event.key)&&toolsMenu.current?.contains(document.activeElement)){event.preventDefault();const items=Array.from(toolsMenu.current.querySelectorAll<HTMLButtonElement>('button'));const index=items.indexOf(document.activeElement as HTMLButtonElement);items[(index+(event.key==='ArrowDown'?1:items.length-1))%items.length]?.focus();}};document.addEventListener('mousedown',pointer);document.addEventListener('keydown',keys);return()=>{document.removeEventListener('mousedown',pointer);document.removeEventListener('keydown',keys);};},[toolsOpen]);
  const [limits, setLimits] = useState<Record<string, number>>({});
  const { page, projects, chats, projectId, chatId, search, activeChatIds } = props;
  const toolGroups = groupWorkspaceTools(props.navItems), currentTool = toolGroups.some(group => group.items.some(item => item.page === page));
  const term = search.toLowerCase();
  const matches = (chat: Chat) => !search || chat.title.toLowerCase().includes(term) || projects.some(project => project.id === chat.project_id && project.name.toLowerCase().includes(term));
  const visible = chats.filter(chat => !chat.archived && matches(chat));
  const archived = chats.filter(chat => chat.archived && matches(chat));
  const isCollapsed=(id:string)=>props.collapsedProjectIds?props.collapsedProjectIds.includes(id):Boolean(collapsed[id]);
  function toggleProject(id:string) {if(props.onCollapsedProjects){const ids=props.collapsedProjectIds || [];props.onCollapsedProjects(ids.includes(id)?ids.filter(value=>value!==id):[...ids,id]);}else setCollapsed(previous=>({...previous,[id]:!previous[id]}));}
  const row = (chat: Chat, recent = false) => <ChatRow key={chat.id} chat={chat} projects={projects} selected={chatId === chat.id && page === 'chat'} active={activeChatIds.includes(chat.id)} recent={recent} onSelect={props.onChat} onChanged={props.onChatChanged} />;
  function rows(scope: string, items: Chat[], defaultLimit: number, recent = false) { const limit = limits[scope] || defaultLimit; return <>{items.slice(0, limit).map(chat => row(chat, recent))}{items.length > limit && <button className="sidebar-show-more" onClick={() => setLimits(previous => ({ ...previous, [scope]: limit + 20 }))}>Show more ({items.length - limit})</button>}</>; }
  return <aside className="sidebar" data-accent={props.accent || 'copper'} style={{width:props.width || 260}}><div className="sidebar-brand"><img src="./forge.svg" alt="" /><span title={props.workspaceName || 'Forge'}>{props.workspaceName || 'Forge'}</span><IconButton showLabel label="Collapse sidebar" onClick={props.onCollapse}><PanelLeftClose size={16} /></IconButton></div>
    <button className="new-chat" onClick={props.onNew}><Plus size={17} />New chat<span>Ctrl N</span></button>
    <div className="sidebar-search"><Search size={14} /><input aria-label="Search projects and chats" placeholder="Search projects and chats" value={search} onChange={event => props.onSearch(event.target.value)} /></div>
    <div className="sidebar-project-header"><button className="sidebar-section-link" onClick={()=>props.onPage('projects')}>Projects</button><IconButton showLabel label="Connect project folder" onClick={props.onConnect}><FolderPlus size={15} /></IconButton></div>
    <div className="project-tree">{projects.filter(project => !term || project.name.toLowerCase().includes(term) || visible.some(chat => chat.project_id === project.id)).map(project => <div className="project-group" key={project.id} data-accent={project.appearance?.accent || props.accent || 'copper'}><div className="project-selection"><button className="project-expand" aria-label={`${isCollapsed(project.id) ? 'Expand' : 'Collapse'} ${project.name}`} aria-expanded={!isCollapsed(project.id)} onClick={() => toggleProject(project.id)}>{isCollapsed(project.id) ? <ChevronRight size={12}/> : <ChevronDown size={12}/>}</button><ProjectActions project={project} onRemoved={props.onProjectRemoved}><button className={`project-link ${project.id === projectId ? 'selected' : ''}`} title={project.name} onClick={() => props.onProject(project.id)}><ProjectIcon project={project} size={17}/><span>{project.name}</span></button></ProjectActions></div>{(!isCollapsed(project.id) || Boolean(search)) && rows(project.id, visible.filter(chat => chat.project_id === project.id), 15)}</div>)}
      {!projects.length && <button className="sidebar-hint" onClick={props.onConnect}><FolderPlus size={15} />Connect a folder</button>}
      <div className="sidebar-project-header"><span>Unassigned chats</span></div>{rows('unassigned', visible.filter(chat => !chat.project_id), 20, true)}
      <button className="archive-heading" aria-expanded={archivesOpen || Boolean(search)} onClick={() => setArchivesOpen(!archivesOpen)}>{archivesOpen || search ? <ChevronDown size={12} /> : <ChevronRight size={12} />}<Archive size={13} /><span>Archived chats</span><span className="archive-count">{archived.length}</span></button>
      {(archivesOpen || search) && <div className="archived-chats">{archived.length ? archived.map(chat => row(chat, true)) : <p>No archived chats.</p>}</div>}
    </div><div className="sidebar-bottom">
      <div className="sidebar-tools" ref={toolsRoot}><button ref={toolsTrigger} className={`nav-item${currentTool ? ' active' : ''}`} aria-haspopup="menu" aria-expanded={toolsOpen} onClick={()=>setToolsOpen(!toolsOpen)}><Wrench size={16} aria-hidden="true"/>Tools<ChevronDown size={13} aria-hidden="true"/></button>
        {toolsOpen && <div ref={toolsMenu} className="workspace-tools-menu" role="menu" aria-label="Workspace tools">{toolGroups.map(group=><div className="workspace-tools-group" role="group" aria-label={group.label} key={group.key}><span aria-hidden="true">{group.label}</span>{group.items.map((item,itemIndex)=><button role="menuitem" key={group.itemKeys[itemIndex]} aria-current={page===item.page?'page':undefined} className={page===item.page?'active':''} onClick={()=>{toolsTrigger.current?.focus();setToolsOpen(false);props.onPage(item.page);}}><item.icon size={16} aria-hidden="true" data-tool={item.page}/>{item.label}{item.page==='scheduled' && Boolean(props.scheduledCount) && <span className="nav-count">{props.scheduledCount}</span>}</button>)}</div>)}</div>}
      </div><button className={`nav-item ${props.settingsOpen ? 'active' : ''}`} aria-haspopup="dialog" aria-expanded={Boolean(props.settingsOpen)} onClick={() => props.onPage('settings')}><Settings2 size={16} />Settings</button>
    </div>{props.onWidth && <ResizeHandle label="Resize sidebar" value={props.width || 260} min={220} max={360} change={props.onWidth}/>}
  </aside>;
}
