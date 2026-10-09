import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { vi, describe, it, expect } from 'vitest';
import { BuilderPage, type BuilderBrief } from './BuilderPage';

const project = {id: 'project', name: 'Fixture project', path: '/fixture'};
const record: BuilderBrief = {id: 'builder', project_id: project.id, title: 'Fixture app', objective: 'Keep ideas locally', audience: 'Readers', constraints: 'Offline', revision: 1, status: 'draft', requirements: [{id: 'r1', text: 'Add an idea', acceptance: 'The list updates'}], gates: {build: {status: 'not_applicable'}, functional: {status: 'pending'}, preview: {status: 'pending'}, artifact: {status: 'not_applicable'}}, previews: []};
function bridge(overrides: Record<string, unknown> = {}) {
  const responses: Record<string, unknown> = {builder_list: {builders: [record]}, builder_get: record, builder_save: {...record, revision: 2}, builder_check: record, builder_changes: {git: true, status: ' M app.js', diff_stat: 'app.js | 2 +-'}, builder_template: {ok: true}, preview_start: {id: 'preview', status: 'starting'}, ...overrides};
  const call = vi.fn(async (action: string, data?: Record<string, unknown>) => {const response = responses[action]; if (response === undefined) throw new Error(`Unexpected action: ${action}`); return typeof response === 'function' ? (response as (data?: unknown) => unknown)(data) : response;});
  window.pywebview = {api: {call}};
  return call;
}
describe('Builder brief and completion evidence', () => {
  it('saves the expected revision and keeps acceptance criteria attached to requirements', async () => {
    const call = bridge(); render(<BuilderPage projects={[project]}/>);
    const objective = await screen.findByLabelText('Objective');
    await userEvent.clear(objective); await userEvent.type(objective, 'A changed objective');
    await userEvent.click(screen.getByRole('button', {name: 'Save brief'}));
    await waitFor(() => expect(call).toHaveBeenCalledWith('builder_save', expect.objectContaining({id: 'builder', expected_revision: 1, objective: 'A changed objective', requirements: record.requirements})));
    expect(await screen.findByText(/Revision 2/)).toBeTruthy();
  });
  it('records manual checks with an explicit observation and does not invent visual verification', async () => {
    const call = bridge(); render(<BuilderPage projects={[project]}/>); await screen.findByLabelText('Objective');
    await userEvent.click(screen.getByRole('button', {name: 'Checks'}));
    expect(screen.getByRole('button', {name: 'Record my check'})).toHaveProperty('disabled', true);
    await userEvent.type(screen.getByLabelText('Inspection evidence'), 'Added an idea and saw the list update.');
    await userEvent.click(screen.getByRole('button', {name: 'Record my check'}));
    await waitFor(() => expect(call).toHaveBeenCalledWith('builder_check', expect.objectContaining({expected_revision: 1, gate: 'functional', source: 'manual', note: 'Added an idea and saw the list update.'})));
    expect(screen.getByText(/inspect visual output separately/)).toBeTruthy();
  });
  it('starts only a loopback-managed preview after an explicit user action and reads real change summaries', async () => {
    const call = bridge(); render(<BuilderPage projects={[project]}/>); await screen.findByLabelText('Objective');
    expect(call.mock.calls.some(([action]) => action === 'preview_start')).toBe(false);
    await userEvent.click(screen.getByRole('button', {name: 'Preview'}));
    await userEvent.click(screen.getByRole('button', {name: 'Start preview'}));
    await waitFor(() => expect(call).toHaveBeenCalledWith('preview_start', {builder_id: 'builder', cwd: 'forge-app', mode: 'static'}));
    await userEvent.click(screen.getByRole('button', {name: 'Changes'}));
    expect(await screen.findByText(/M app.js/)).toBeTruthy();
  });
  it('surfaces revision conflicts without replacing the editable draft', async () => {
    bridge({builder_save: {error: 'Builder brief changed. Reload before saving this revision.'}}); render(<BuilderPage projects={[project]}/>); await screen.findByLabelText('Objective');
    await userEvent.type(screen.getByLabelText('Objective'), ' Preserve this.'); await userEvent.click(screen.getByRole('button', {name: 'Save brief'}));
    expect(await screen.findByRole('alert')).toHaveProperty('textContent', 'Builder brief changed. Reload before saving this revision.');
    expect(screen.getByLabelText('Objective')).toHaveProperty('value', 'Keep ideas locally Preserve this.');
  });
  it('creates real export requests and uses the native Save dialog for verified downloads', async () => {
    const artifact = {id: 'artifact', path: 'exports/report.pdf', sha256: 'fixture-hash', status: 'structure_verified', visual_verified: false};
    const call = bridge({document_create: {ok: true}, artifact_save_as: {ok: true}, builder_get: {...record, artifacts: [artifact]}});
    render(<BuilderPage projects={[project]}/>); await screen.findByLabelText('Objective');
    await userEvent.click(screen.getByRole('button', {name: 'Checks'}));
    await userEvent.type(screen.getByLabelText('Export text'), 'A verified document body.');
    await userEvent.click(screen.getByRole('button', {name: 'Create and inspect export'}));
    await waitFor(() => expect(call).toHaveBeenCalledWith('document_create', expect.objectContaining({project_id: 'project', builder_id: 'builder', path: 'exports/report.pdf', text: 'A verified document body.', expected_sha256: 'missing'})));
    await userEvent.click(screen.getByRole('button', {name: 'Download'}));
    await waitFor(() => expect(call).toHaveBeenCalledWith('artifact_save_as', {id: 'artifact'}));
    expect(screen.getByText(/Visual inspection pending/)).toBeTruthy();
  });
  it('keeps the native preview inside its owner viewport and updates after parent scrolling', async () => {
    let top = 300;
    const preview = {id: 'preview', builder_id: 'builder', status: 'ready', url: 'http://127.0.0.1:1234/'};
    const call = bridge({builder_get: {...record, previews: [preview]}, preview_browser_status: {builder_id: 'builder'}, preview_browser_show: {ok: true}, preview_browser_hide: {ok: true}, preview_status: {previews: [preview]}});
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function(this: HTMLElement) {
      const height = this.id === 'forge-preview-panel' ? Number.parseInt(this.style.height) || 735 : 0;
      return {x: 100, y: top, top, left: 100, width: 500, height, right: 600, bottom: top + height, toJSON: () => ({})} as DOMRect;
    });
    render(<BuilderPage projects={[project]}/>); await screen.findByLabelText('Objective');
    await userEvent.click(screen.getByRole('button', {name: 'Preview'}));
    await waitFor(() => expect(call).toHaveBeenCalledWith('preview_browser_show', expect.objectContaining({id: 'preview', rect: expect.objectContaining({y: 300, height: innerHeight - 328})})));
    top = 220; fireEvent.scroll(window);
    await waitFor(() => expect(call).toHaveBeenCalledWith('preview_browser_show', expect.objectContaining({rect: expect.objectContaining({y: 220, height: innerHeight - 248})})));
  });
});
