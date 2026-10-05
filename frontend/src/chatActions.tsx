import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { Archive, ArchiveRestore, FolderInput, MessageSquare, MoreHorizontal, Pencil, Trash2 } from 'lucide-react';
import { api, errorText } from './api';
import { ErrorNotice, Field, Modal } from './components';
import type { Chat, Project } from './types';

export type ChatChange = 'move' | 'rename' | 'archive' | 'restore' | 'delete';
interface ChatRowProps {
  chat: Chat; projects: Project[]; selected: boolean; active: boolean; recent?: boolean;
  onSelect: (id: string) => void; onChanged: (id: string, change: ChatChange) => Promise<void>;
}
export function ChatRow({ chat, projects, selected, active, recent = false, onSelect, onChanged }: ChatRowProps) {
  const [menu, setMenu] = useState(false); const [dialog, setDialog] = useState<ChatChange | null>(null);
  const [project, setProject] = useState(chat.project_id || ''); const [title, setTitle] = useState(chat.title);
  const [busy, setBusy] = useState(false); const [error, setError] = useState('');
  const [position, setPosition] = useState({ top: 0, left: 0 }); const menuRoot = useRef<HTMLDivElement>(null);
  const root = useRef<HTMLDivElement>(null); const trigger = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!menu) return;
    menuRoot.current?.querySelector<HTMLButtonElement>('button')?.focus();
    const click = (event: MouseEvent) => { if (!root.current?.contains(event.target as Node) && !menuRoot.current?.contains(event.target as Node)) setMenu(false); };
    const keys = (event: KeyboardEvent) => { if (event.key === 'Escape') { setMenu(false); trigger.current?.focus(); } else if (['ArrowDown', 'ArrowUp'].includes(event.key) && menuRoot.current?.contains(document.activeElement)) { event.preventDefault(); const items = Array.from(menuRoot.current.querySelectorAll<HTMLButtonElement>('button:not(:disabled)')); const index = items.indexOf(document.activeElement as HTMLButtonElement); items[(index + (event.key === 'ArrowDown' ? 1 : items.length - 1)) % items.length]?.focus(); } };
    document.addEventListener('mousedown', click); document.addEventListener('keydown', keys);
    return () => { document.removeEventListener('mousedown', click); document.removeEventListener('keydown', keys); };
  }, [menu]);
  function open(action: ChatChange) { setMenu(false); setError(''); setTitle(chat.title); setProject(chat.project_id || ''); setDialog(action); }
  async function save() {
    if (!dialog) return; setBusy(true); setError('');
    try {
      const actions = { move: 'chat_move', rename: 'rename_chat', archive: 'chat_archive', restore: 'chat_archive', delete: 'chat_delete' };
      const data = dialog === 'move' ? { id: chat.id, project_id: project || null } : dialog === 'rename' ? { id: chat.id, title: title.trim() } : dialog === 'archive' || dialog === 'restore' ? { id: chat.id, archived: dialog === 'archive' } : { id: chat.id };
      await api(actions[dialog], data); await onChanged(chat.id, dialog); setDialog(null);
    } catch (e) { setError(errorText(e)); } finally { setBusy(false); }
  }
  const name = chat.title || 'New chat';
  return <div className={`chat-row ${selected ? 'selected' : ''}`} ref={root} onContextMenu={event => { event.preventDefault(); setPosition({ top: Math.max(8, Math.min(window.innerHeight - 220, event.clientY)), left: Math.max(8, Math.min(window.innerWidth - 220, event.clientX)) }); setMenu(true); }}>
    <button className={`chat-link ${recent ? 'recent' : ''} ${selected ? 'active' : ''}`} onClick={() => { setMenu(false); onSelect(chat.id); }} title={name}>{recent && <MessageSquare size={13} />}<span>{name}</span>{active && <i className="live-dot" />}{chat.archived && <Archive size={12} />}</button>
    <button ref={trigger} className="chat-actions-trigger" aria-label={`Chat actions for ${name}`} title="Chat actions" aria-haspopup="menu" aria-expanded={menu} onClick={() => { const rect = trigger.current?.getBoundingClientRect(); if (rect) setPosition({ top: Math.max(8, Math.min(window.innerHeight - 220, rect.bottom + 5)), left: Math.max(8, Math.min(window.innerWidth - 220, rect.right - 210)) }); setMenu(!menu); }}><MoreHorizontal size={15} /></button>
    {menu && createPortal(<div ref={menuRoot} style={position} className="chat-actions-menu" role="menu" aria-label={`Actions for ${name}`}>
      <button role="menuitem" onClick={() => open('rename')}><Pencil size={14} />Rename</button>
      <button role="menuitem" onClick={() => open('move')}><FolderInput size={14} />{chat.project_id ? 'Move to project' : 'Add to project'}</button>
      <button role="menuitem" disabled={active} onClick={() => open(chat.archived ? 'restore' : 'archive')}>{chat.archived ? <ArchiveRestore size={14} /> : <Archive size={14} />}{chat.archived ? 'Restore chat' : 'Archive chat'}</button>
      <button role="menuitem" className="menu-danger" onClick={() => open('delete')}><Trash2 size={14} />Delete chat</button>
      {active && <small>Stop the active run before archiving. Moving or deleting stops unfinished work.</small>}
    </div>, document.body)}
    {dialog && <Modal title={dialog === 'move' ? 'Move conversation' : dialog === 'rename' ? 'Rename conversation' : dialog === 'delete' ? 'Delete conversation?' : dialog === 'restore' ? 'Restore conversation?' : 'Archive conversation?'} close={() => { if (!busy) setDialog(null); }}>
      <form onSubmit={event => { event.preventDefault(); void save(); }}>
        {dialog === 'move' ? <><p className="muted">Choose where to keep <strong>{name}</strong>. Its messages and saved progress remain together. Unfinished work will be stopped before moving this chat.</p><Field label="Project"><select value={project} onChange={event => setProject(event.target.value)}><option value="">Personal workspace</option>{projects.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></Field></> : dialog === 'rename' ? <Field label="Conversation title"><input autoFocus required maxLength={200} value={title} onChange={event => setTitle(event.target.value)} /></Field> : <p>{dialog === 'delete' ? <>Permanently delete <strong>{name}</strong> and its saved messages? Unfinished work will be stopped. Recorded usage totals are retained.</> : dialog === 'restore' ? <>Return <strong>{name}</strong> to your chat list?</> : <>Move <strong>{name}</strong> to Archived chats? You can restore it later.</>}</p>}
        {error && <ErrorNotice error={error} />}<footer><button type="button" className="subtle" disabled={busy} onClick={() => setDialog(null)}>Cancel</button><button className={dialog === 'delete' ? 'danger-button' : 'primary'} disabled={busy || (dialog === 'rename' && !title.trim())}>{busy ? 'Saving…' : dialog === 'delete' ? 'Delete permanently' : dialog === 'restore' ? 'Restore chat' : dialog === 'archive' ? 'Archive chat' : 'Save'}</button></footer>
      </form>
    </Modal>}
  </div>;
}
