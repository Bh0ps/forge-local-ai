import { useState } from 'react';
import { Archive, ChevronDown, ChevronRight, Folder, FolderPlus, PanelLeftClose, Plus, Search, Settings2 } from 'lucide-react';
import { IconButton } from './components';
import { ChatRow } from './chatActions';
import { ProjectActions } from './projectActions';
import type { ChatChange } from './chatActions';
import type { Chat, Page, Project } from './types';
interface SidebarProps {
  page: Page; projects: Project[]; chats: Chat[]; projectId: string | null; chatId: string | null;
  search: string; onSearch: (value: string) => void; activeChatIds: string[]; scheduledCount?: number;
  navItems: { page: Page; label: string; icon: typeof Folder }[];
  onPage: (page: Page) => void; onNew: () => void; onCollapse: () => void; onConnect: () => void;
  onProject: (id: string | null) => void; onChat: (id: string) => void;
  onChatChanged: (id: string, change: ChatChange) => Promise<void>;
  onProjectRemoved: (id: string) => Promise<void>;
}
export function WorkspaceSidebar(props: SidebarProps) {
  const [archivesOpen, setArchivesOpen] = useState(false);
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});
  const [limits, setLimits] = useState<Record<string, number>>({});
  const { page, projects, chats, projectId, chatId, search, activeChatIds } = props;
  const term = search.toLowerCase();
  const matches = (chat: Chat) => !search || chat.title.toLowerCase().includes(term) || projects.some(project => project.id === chat.project_id && project.name.toLowerCase().includes(term));
  const visible = chats.filter(chat => !chat.archived && matches(chat));
  const archived = chats.filter(chat => chat.archived && matches(chat));
  const row = (chat: Chat, recent = false) => <ChatRow key={chat.id} chat={chat} projects={projects} selected={chatId === chat.id && page === 'chat'} active={activeChatIds.includes(chat.id)} recent={recent} onSelect={props.onChat} onChanged={props.onChatChanged} />;
  function rows(scope: string, items: Chat[], defaultLimit: number, recent = false) { const limit = limits[scope] || defaultLimit; return <>{items.slice(0, limit).map(chat => row(chat, recent))}{items.length > limit && <button className="sidebar-show-more" onClick={() => setLimits(previous => ({ ...previous, [scope]: limit + 20 }))}>Show more ({items.length - limit})</button>}</>; }
  return <aside className="sidebar"><div className="sidebar-brand"><img src="./forge.svg" alt="" /><span>Forge</span><span className="version">5.0.2</span><IconButton label="Collapse sidebar" onClick={props.onCollapse}><PanelLeftClose size={17} /></IconButton></div>
    <button className="new-chat" onClick={props.onNew}><Plus size={17} />New chat<span>Ctrl N</span></button>
    <div className="sidebar-search"><Search size={14} /><input aria-label="Search projects and chats" placeholder="Search projects and chats" value={search} onChange={event => props.onSearch(event.target.value)} /></div>
    <nav aria-label="Workspace navigation">{props.navItems.map(item => <button className={`nav-item ${page === item.page ? 'active' : ''}`} key={item.page} onClick={() => props.onPage(item.page)}><item.icon size={16} />{item.label}{item.page === 'scheduled' && Boolean(props.scheduledCount) && <span className="nav-count">{props.scheduledCount}</span>}</button>)}</nav>
    <div className="sidebar-project-header"><span>Projects</span><IconButton label="Connect project folder" onClick={props.onConnect}><FolderPlus size={15} /></IconButton></div>
    <div className="project-tree">{projects.filter(project => !term || project.name.toLowerCase().includes(term) || visible.some(chat => chat.project_id === project.id)).map(project => <div className="project-group" key={project.id}><div className="project-selection"><button className="project-expand" aria-label={`${collapsed[project.id] ? 'Expand' : 'Collapse'} ${project.name}`} aria-expanded={!collapsed[project.id]} onClick={() => setCollapsed(previous => ({ ...previous, [project.id]: !previous[project.id] }))}>{collapsed[project.id] ? <ChevronRight size={12}/> : <ChevronDown size={12}/>}</button><ProjectActions project={project} onRemoved={props.onProjectRemoved}><button className={`project-link ${project.id === projectId ? 'selected' : ''}`} onClick={() => props.onProject(project.id)}><Folder size={15} /><span>{project.name}</span></button></ProjectActions></div>{(!collapsed[project.id] || Boolean(search)) && rows(project.id, visible.filter(chat => chat.project_id === project.id), 15)}</div>)}
      {!projects.length && <button className="sidebar-hint" onClick={props.onConnect}><FolderPlus size={15} />Connect a folder</button>}
      <div className="sidebar-project-header"><span>Unassigned chats</span></div>{rows('unassigned', visible.filter(chat => !chat.project_id), 20, true)}
      <button className="archive-heading" aria-expanded={archivesOpen || Boolean(search)} onClick={() => setArchivesOpen(!archivesOpen)}>{archivesOpen || search ? <ChevronDown size={12} /> : <ChevronRight size={12} />}<Archive size={13} /><span>Archived chats</span><span className="archive-count">{archived.length}</span></button>
      {(archivesOpen || search) && <div className="archived-chats">{archived.length ? archived.map(chat => row(chat, true)) : <p>No archived chats.</p>}</div>}
    </div><div className="sidebar-bottom"><button className={`nav-item ${page === 'settings' ? 'active' : ''}`} onClick={() => props.onPage('settings')}><Settings2 size={16} />Settings</button></div>
  </aside>;
}
