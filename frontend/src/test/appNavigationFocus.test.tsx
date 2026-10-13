import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import App from '../App';
import { DEFAULT_SETTINGS } from '../types';
import { DEFAULT_LAYOUT } from '../workspaceLayout';
import { updateReadiness } from '../updateReadiness';

afterEach(() => { updateReadiness.dismiss(); delete window.pywebview; });

async function workspace() {
  const chat = { id: 'keyboard-focus-chat', title: 'Keyboard fixture', project_id: null, model: 'fixture', messages: [{ role: 'assistant', content: 'Keyboard fixture answer' }] };
  const drafts = new Map<string, { revision: number; content: unknown; scope_available: boolean }>([[chat.id, { revision: 1, content: { text: 'Saved keyboard fixture draft', document_ids: [], skills: [], space_ids: [] }, scope_available: true }]]);
  const call = vi.fn(async (action: string, data: Record<string, unknown> = {}) => {
    if (action === 'bootstrap') return { settings: { ...DEFAULT_SETTINGS, model: 'fixture' }, projects: [], chats: [chat], runs: [], capabilities: {} };
    if (action === 'get_chat') return chat;
    if (action === 'setup_status') return { first_run: false };
    if (action === 'ui_layout_get') return DEFAULT_LAYOUT;
    if (action === 'ui_layout_save') return data.layout;
    const key = typeof data.chat_id === 'string' ? data.chat_id : 'new';
    const current = drafts.get(key) || { revision: 0, content: {}, scope_available: true };
    if (action === 'draft_get') return current;
    if (action === 'draft_save') {
      if (data.expected_revision !== current.revision) return { conflict: true, draft: current };
      const next = { revision: current.revision + 1, content: data.content, scope_available: true };
      drafts.set(key, next);
      return next;
    }
    if (action === 'pending_questions') return { questions: [] };
    if (['projects', 'chats', 'runs', 'schedules', 'spaces', 'models'].includes(action)) return { [action]: action === 'chats' ? [chat] : [] };
    return {};
  });
  window.pywebview = { api: { call } };
  render(<App />);
  await act(async () => window.dispatchEvent(new Event('pywebviewready')));
  await screen.findByText('Keyboard fixture answer');
  await waitFor(() => expect((screen.getByRole('textbox', { name: 'Message Forge' }) as HTMLTextAreaElement).value).toBe('Saved keyboard fixture draft'));
  return call;
}

describe('new conversation keyboard focus', () => {
  it('focuses the newly mounted composer from Settings and preserves the previous chat draft', async () => {
    await workspace();
    await userEvent.click(screen.getByRole('button', { name: 'Settings' }));
    (await screen.findByRole('textbox', { name: 'Find a setting' })).focus();
    await userEvent.keyboard('{Control>}n{/Control}');
    const composer = await screen.findByRole('textbox', { name: 'Message Forge' });
    expect(document.activeElement).toBe(composer);
    expect(screen.queryByText('Keyboard fixture answer')).toBeNull();
    await userEvent.click(screen.getByRole('button', { name: 'Keyboard fixture' }));
    await screen.findByText('Keyboard fixture answer');
    await waitFor(() => expect((screen.getByRole('textbox', { name: 'Message Forge' }) as HTMLTextAreaElement).value).toBe('Saved keyboard fixture draft'));
  });

  it('keeps focus and the current page when navigation is refused', async () => {
    await workspace();
    await userEvent.click(screen.getByRole('button', { name: 'Settings' }));
    const search = await screen.findByRole('textbox', { name: 'Find a setting' });
    search.focus();
    const deny = (event: Event) => event.preventDefault();
    window.addEventListener('forge:navigate', deny);
    try { await userEvent.keyboard('{Control>}n{/Control}'); }
    finally { window.removeEventListener('forge:navigate', deny); }
    expect(document.activeElement).toBe(search);
    expect(screen.queryByRole('textbox', { name: 'Message Forge' })).toBeNull();
  });

  it('focuses an already mounted new composer on repeated New chat', async () => {
    await workspace();
    await userEvent.click(screen.getByRole('button', { name: /New chat/ }));
    const button = screen.getByRole('button', { name: /New chat.*Ctrl N/ });
    button.focus();
    fireEvent.keyDown(window, { key: 'n', ctrlKey: true });
    expect(document.activeElement).toBe(screen.getByRole('textbox', { name: 'Message Forge' }));
  });
});
