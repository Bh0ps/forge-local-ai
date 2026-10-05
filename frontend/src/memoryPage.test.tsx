import { fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { MemoryPage } from './memoryPage';
import { DEFAULT_SETTINGS } from './types';

const memory = { id: 'memory_fixture', title: 'Use Ruff', content: 'Format Python with Ruff.', kind: 'convention', scope: 'global', status: 'pending', revision: 1 };
const skill = { id: 'skill_fixture', name: 'review-workflow', description: 'Review isolated changes.', markdown: 'Inspect then verify.', scope: 'global', status: 'pending', revision: 1, license: 'MIT' };
function setup() {
  const call = vi.fn(async (action: string) => {
    if (action === 'memory_list') return { items: [memory], skills: [skill, { ...skill, id: 'promoted_fixture', name: 'promoted-workflow', status: 'promoted' }] };
    if (action === 'agents') return { agents: [] };
    throw new Error(`Unexpected action ${action}`);
  });
  window.pywebview = { api: { call } };
  const change = vi.fn(async () => {});
  render(<MemoryPage projects={[]} settings={DEFAULT_SETTINGS} onChange={change} notify={vi.fn()} />);
  return { call, change };
}

describe('memory review layout', () => {
  it('keeps category and preferences in the right rail and saved content in review', async () => {
    const { change } = setup();
    const review = screen.getByRole('region', { name: 'Saved memory review' });
    const controls = screen.getByRole('complementary', { name: 'Memory preferences' });
    expect(await within(review).findByText('Use Ruff')).toBeTruthy();
    expect(within(review).getByRole('heading', { name: 'Awaiting review' })).toBeTruthy();
    expect(within(review).queryByRole('switch')).toBeNull();
    expect(within(controls).queryByText('Use Ruff')).toBeNull();
    await userEvent.click(within(controls).getByRole('switch', { name: 'Recall approved memory' }));
    expect(change).toHaveBeenCalledExactlyOnceWith({ memory_enabled: false });
    await userEvent.click(within(controls).getByRole('button', { name: /Learned skills/ }));
    expect(within(review).getByText('review-workflow')).toBeTruthy();
    expect(within(review).queryByText('promoted-workflow')).toBeNull();
    expect(within(review).queryByText('Use Ruff')).toBeNull();
    expect(within(controls).getByRole('button', { name: /Learned skills/ }).getAttribute('aria-pressed')).toBe('true');
  });

  it('preserves search and filters when switching categories and explains empty searches', async () => {
    setup();
    await screen.findByText('Use Ruff');
    fireEvent.change(screen.getByLabelText('Filter memory'), { target: { value: 'missing fixture' } });
    expect(screen.getByText('No matching saves')).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: /Learned skills/ }));
    expect((screen.getByLabelText('Filter memory') as HTMLInputElement).value).toBe('missing fixture');
    expect(screen.getByText('No matching saves')).toBeTruthy();
    fireEvent.change(screen.getByLabelText('Filter memory'), { target: { value: 'review' } });
    expect(screen.getByText('review-workflow')).toBeTruthy();
    await userEvent.selectOptions(screen.getByLabelText('Memory status'), 'approved');
    expect(await screen.findByText('promoted-workflow')).toBeTruthy();
    expect(screen.queryByText('review-workflow')).toBeNull();
  });
});
