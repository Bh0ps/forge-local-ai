import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { SkillSelector, SkillWorkbench } from '../skillWorkbench';

const initial = { skill: { id: 'rule', name: 'Frontend workflow', enabled: true, automatic: true, builtin: true, scope: 'global' }, text: '---\nname: frontend\n---\n# Frontend workflow\nRead current components.', revision: 'a'.repeat(64), digest: 'b'.repeat(64), manifest: { schema_version: 1, phases: ['implement'], resources: ['references/example.md'] }, resources: ['references/example.md'] };
function bridge(overrides: Record<string, unknown | ((data: Record<string, unknown>) => unknown)> = {}) {
  const responses: Record<string, unknown> = { skill_preview: initial, skill_route_preview: { phase: 'implement', selected: [{ id: 'rule', name: 'Frontend workflow', selection_reason: 'Matches ui during implement' }] }, skill_versions: { versions: [{ revision: 'c'.repeat(64), saved_at: 1700000000, operation: 'before edit' }] }, skills_resource_read: { text: '# Example\nUse a fresh component.', truncated: false }, ...overrides };
  const call = vi.fn(async (action: string, data: Record<string, unknown> = {}) => { if (!(action in responses)) throw new Error(`Unexpected action ${action}`); const value = responses[action]; return typeof value === 'function' ? value(data) : value; });
  window.pywebview = { api: { call } }; return { call, responses };
}
function workbench(onSaved = vi.fn(async () => {})) { render(<SkillWorkbench id="rule" projectId="project-1" close={vi.fn()} onSaved={onSaved} notify={vi.fn()} />); return onSaved; }

describe('skill workbench', () => {
  it('previews guidance and reads a confined resource without modifying configuration', async () => {
    const { call } = bridge(); workbench(); await screen.findByText('Read current components.');
    await userEvent.click(screen.getByRole('button', { name: 'references/example.md' })); await screen.findByText('Use a fresh component.');
    expect(call).toHaveBeenCalledWith('skills_resource_read', { id: 'rule', project_id: 'project-1', path: 'references/example.md', limit: 16000 });
    expect(call.mock.calls.some(([action]) => action === 'skill_edit' || action === 'skill_toggle')).toBe(false);
  });
  it('saves instructions with the current revision and refreshes the collection', async () => {
    const { call } = bridge({ skill_edit: { ...initial, text: initial.text + '\nKeep focus visible.', revision: 'd'.repeat(64) } }); const refreshed = workbench();
    await screen.findByText('Read current components.'); await userEvent.click(screen.getByRole('button', { name: 'Edit' }));
    await userEvent.type(screen.getByLabelText('Skill instructions'), '\nKeep focus visible.'); await userEvent.click(screen.getByRole('button', { name: 'Save skill' }));
    await waitFor(() => expect(call).toHaveBeenCalledWith('skill_edit', { id: 'rule', project_id: 'project-1', expected_revision: initial.revision, text: initial.text + '\nKeep focus visible.', manifest: initial.manifest }));
    await waitFor(() => expect(refreshed).toHaveBeenCalled());
  });
  it('shows a stale edit error and retains the edited draft', async () => {
    bridge({ skill_edit: () => { throw new Error('Skill changed since you opened it. Reload before saving.'); } }); workbench();
    await screen.findByText('Read current components.'); await userEvent.click(screen.getByRole('button', { name: 'Edit' }));
    await userEvent.type(screen.getByLabelText('Skill instructions'), '\nDraft rule.'); await userEvent.click(screen.getByRole('button', { name: 'Save skill' }));
    await screen.findByText('Skill changed since you opened it. Reload before saving.'); expect((screen.getByLabelText('Skill instructions') as HTMLTextAreaElement).value).toContain('Draft rule.');
  });
  it('tests task routing without activating or enabling a skill', async () => {
    const { call } = bridge(); workbench(); await screen.findByText('Read current components.'); await userEvent.click(screen.getByRole('button', { name: 'Test routing' }));
    await userEvent.type(screen.getByLabelText('Example task'), 'Add a React component'); await userEvent.click(screen.getByRole('button', { name: 'Preview skill selection' }));
    await screen.findByText('Matches ui during implement'); expect(call).toHaveBeenCalledWith('skill_route_preview', { query: 'Add a React component', project_id: 'project-1' });
    expect(call.mock.calls.some(([action]) => action === 'skill_toggle')).toBe(false);
  });
  it('restores history with optimistic revision checking', async () => {
    const { call } = bridge({ skill_restore: initial }); workbench(); await screen.findByText('Read current components.'); await userEvent.click(screen.getByRole('button', { name: 'Versions' }));
    await screen.findByText('before edit'); await userEvent.click(screen.getByRole('button', { name: 'Restore' }));
    await waitFor(() => expect(call).toHaveBeenCalledWith('skill_restore', { id: 'rule', project_id: 'project-1', expected_revision: initial.revision, revision: 'c'.repeat(64) }));
  });
  it('ignores a prior scope response after switching projects', async () => {
    let old: (value: unknown) => void = () => {};
    bridge({ skill_preview: (data: Record<string, unknown>) => data.project_id === 'old' ? new Promise(resolve => { old = resolve; }) : { ...initial, text: '# New project\nCurrent scope.' } });
    const view = render(<SkillWorkbench id="rule" projectId="old" close={vi.fn()} onSaved={vi.fn(async () => {})} notify={vi.fn()} />);
    view.rerender(<SkillWorkbench id="rule" projectId="new" close={vi.fn()} onSaved={vi.fn(async () => {})} notify={vi.fn()} />);
    await screen.findByText('Current scope.'); await act(async () => old(initial)); expect(screen.queryByText('Read current components.')).toBeNull();
  });
});

describe('skill selector', () => {
  it('selects stable IDs, identifies disabled/unavailable entries and removes stale selections', async () => {
    const { call } = bridge({ skills: { skills: [{ id: 'rule', name: 'Frontend workflow', enabled: true, automatic: true }, { id: 'off', name: 'Disabled workflow', enabled: false, automatic: false }] } });
    const change = vi.fn(); render(<SkillSelector value={['missing']} onChange={change} projectId="project-1" />);
    await screen.findByText('Frontend workflow'); expect((screen.getByRole('checkbox', { name: /Disabled workflow/ }) as HTMLInputElement).disabled).toBe(true);
    await userEvent.click(screen.getByRole('checkbox', { name: /Frontend workflow/ })); expect(change).toHaveBeenCalledWith(['missing', 'rule']);
    await userEvent.click(screen.getByRole('button', { name: 'missing ×' })); expect(change).toHaveBeenCalledWith([]);
    expect(call).toHaveBeenCalledWith('skills', { project_id: 'project-1' });
  });
});
