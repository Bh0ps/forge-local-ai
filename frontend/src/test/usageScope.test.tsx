import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { UsagePage } from '../pages';
import type { Model, Project } from '../types';

type Read = { data: Record<string, unknown>; resolve: (value: unknown) => void; reject: (reason: unknown) => void };
const models: Model[] = [{ name: 'fixture-a' }, { name: 'fixture-b' }];
const projects: Project[] = [{ id: 'fixture-project', name: 'Fixture project', path: 'fixture-folder' }];
function response(input = 0, output = 0, requests = 0, extra = {}) {
  return { totals: { input_tokens: input, output_tokens: output, cached_input_tokens: 0, requests }, daily: [], by_model: [], timezone: 'UTC', ...extra };
}
function heldBridge() {
  const pending: Read[] = [];
  const call = vi.fn(async (action: string, data: Record<string, unknown> = {}) => {
    if (action !== 'usage') throw new Error('Unexpected synthetic action');
    return new Promise((resolve, reject) => pending.push({ data, resolve, reject }));
  });
  const bridge = { call };
  window.pywebview = { api: bridge };
  return { call, bridge, pending };
}
async function batch(pending: Read[], from = 0) {
  await waitFor(() => expect(pending.length).toBeGreaterThanOrEqual(from + 4));
  return pending.slice(from, from + 4);
}
async function answer(reads: Read[], value: unknown) { await act(async () => { for (const read of reads) read.resolve(value); }); }
async function fail(reads: Read[], message: string) { await act(async () => { reads[0].reject(new Error(message)); }); }
function inputText() { return document.querySelector('.usage-breakdown > div:first-child strong')?.textContent; }
function accounting() { return screen.getByRole('region', { name: 'Usage accounting' }); }

afterEach(() => { vi.unstubAllGlobals(); });

describe('Usage response ownership', () => {
  it('keeps model B900 after the preserved older model A100 response', async () => {
    const owner = heldBridge(); render(<UsagePage projects={projects} models={models} timezone="UTC" />);
    await batch(owner.pending);
    fireEvent.change(screen.getByRole('combobox', { name: 'Filter usage by model' }), { target: { value: 'fixture-a' } });
    const older = await batch(owner.pending, 4);
    fireEvent.change(screen.getByRole('combobox', { name: 'Filter usage by model' }), { target: { value: 'fixture-b' } });
    const current = await batch(owner.pending, 8);
    await answer(current, response(900, 0, 1)); expect(inputText()).toBe('900');
    await answer(older, response(100, 0, 1));
    expect((screen.getByRole('combobox', { name: 'Filter usage by model' }) as HTMLSelectElement).value).toBe('fixture-b');
    expect(inputText()).toBe('900');
  });

  it.each(['period', 'project', 'timezone'])('does not publish an earlier %s scope', async boundary => {
    const owner = heldBridge(); const view = render(<UsagePage projects={projects} models={models} timezone="UTC" />);
    const older = await batch(owner.pending);
    if (boundary === 'period') fireEvent.click(screen.getByRole('button', { name: 'Day', exact: true }));
    if (boundary === 'project') fireEvent.change(screen.getByRole('combobox', { name: 'Filter usage by project' }), { target: { value: 'fixture-project' } });
    if (boundary === 'timezone') view.rerender(<UsagePage projects={projects} models={models} timezone="Europe/Dublin" />);
    const current = await batch(owner.pending, 4);
    await answer(older, response(100));
    expect(inputText()).toBe('—'); expect(accounting().getAttribute('aria-busy')).toBe('true');
    await answer(current, response(900, 0, 1)); expect(inputText()).toBe('900');
    expect(accounting().getAttribute('aria-busy')).toBe('false');
  });

  it('retires an old A scope even when the selected model returns to A', async () => {
    const owner = heldBridge(); render(<UsagePage projects={projects} models={models} timezone="UTC" />);
    await batch(owner.pending);
    const select = screen.getByRole('combobox', { name: 'Filter usage by model' });
    fireEvent.change(select, { target: { value: 'fixture-a' } }); const olderA = await batch(owner.pending, 4);
    fireEvent.change(select, { target: { value: 'fixture-b' } }); await batch(owner.pending, 8);
    fireEvent.change(select, { target: { value: 'fixture-a' } }); const currentA = await batch(owner.pending, 12);
    await answer(currentA, response(900, 0, 1)); await answer(olderA, response(100, 0, 1));
    expect(inputText()).toBe('900'); expect(accounting().getAttribute('aria-busy')).toBe('false');
  });

  it.each(['success', 'error'])('ignores older same-scope %s and finally while the current refresh waits', async outcome => {
    const owner = heldBridge(); render(<UsagePage projects={projects} models={models} timezone="UTC" />);
    const older = await batch(owner.pending);
    fireEvent.click(screen.getByRole('button', { name: 'Refresh usage' })); const current = await batch(owner.pending, 4);
    if (outcome === 'success') await answer(older, response(100)); else await fail(older, 'Old fixture failure');
    expect(inputText()).toBe('—'); expect(accounting().getAttribute('aria-busy')).toBe('true');
    expect(screen.queryByRole('alert')).toBeNull();
    await answer(current, response(900, 0, 1)); expect(inputText()).toBe('900');
  });

  it('does not mix partially completed selected and card responses', async () => {
    const owner = heldBridge(); render(<UsagePage projects={projects} models={models} timezone="UTC" />);
    const reads = await batch(owner.pending);
    await answer(reads.slice(0, 3), response(900, 0, 1));
    expect(inputText()).toBe('—'); expect(accounting().getAttribute('aria-busy')).toBe('true');
    await answer(reads.slice(3), response(500, 0, 2));
    expect(inputText()).toBe('900');
    expect(within(screen.getByRole('article', { name: 'All time' })).getByText('500')).toBeTruthy();
  });

  it('retains the current failure and Retry when an older successful refresh arrives', async () => {
    const owner = heldBridge(); render(<UsagePage projects={projects} models={models} timezone="UTC" />);
    const older = await batch(owner.pending);
    fireEvent.click(screen.getByRole('button', { name: 'Refresh usage' }));
    const current = await batch(owner.pending, 4);
    await fail(current, 'Current fixture failure');
    await answer(older, response(100));
    expect(screen.getByRole('alert').textContent).toContain('Current fixture failure');
    expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy();
    expect(inputText()).toBe('—'); expect(accounting().getAttribute('aria-busy')).toBe('false');
  });

  it('preserves the exact four usage payloads and selected/calendar card contract', async () => {
    const owner = heldBridge(); render(<UsagePage projects={projects} models={models} timezone="UTC" />);
    await batch(owner.pending);
    expect(owner.call.mock.calls).toEqual(['month', 'day', 'month', 'all'].map(period => ['usage', { period, timezone: 'UTC', model: '', project_id: undefined }]));
    fireEvent.change(screen.getByRole('combobox', { name: 'Filter usage by model' }), { target: { value: 'fixture-b' } });
    await batch(owner.pending, 4);
    fireEvent.change(screen.getByRole('combobox', { name: 'Filter usage by project' }), { target: { value: 'fixture-project' } });
    await batch(owner.pending, 8);
    expect(owner.call.mock.calls.slice(8)).toEqual(['month', 'day', 'month', 'all'].map(period => ['usage', { period, timezone: 'UTC', model: 'fixture-b', project_id: 'fixture-project' }]));
  });

  it.each(['bridge', 'call'])('rejects a replaced native %s before rerender then accepts its new read batch', async boundary => {
    const owner = heldBridge(); const view = render(<UsagePage projects={projects} models={models} timezone="UTC" />);
    const older = await batch(owner.pending);
    const current = heldBridge();
    if (boundary === 'call') {
      owner.bridge.call = current.call; window.pywebview = { api: owner.bridge };
    }
    await answer(older, response(100)); expect(inputText()).toBe('—');
    view.rerender(<UsagePage projects={projects} models={models} timezone="UTC" />);
    await answer(await batch(current.pending), response(900, 0, 1)); expect(inputText()).toBe('900');
  });

  it('rejects a replaced browser transport before rerender and retains the read-only payload', async () => {
    delete window.pywebview;
    const pending: { data: Record<string, unknown>; resolve: (response: Response) => void }[] = [];
    const fetch = vi.fn(async (_url: string, options: RequestInit) => new Promise<Response>(resolve => pending.push({ data: JSON.parse(String(options.body)), resolve })));
    vi.stubGlobal('fetch', fetch);
    const view = render(<UsagePage projects={projects} models={models} timezone="UTC" />);
    await waitFor(() => expect(pending).toHaveLength(4));
    const newer = vi.fn(async () => new Response(JSON.stringify(response(900, 0, 1)), { status: 200, headers: { 'Content-Type': 'application/json' } }));
    vi.stubGlobal('fetch', newer);
    await act(async () => { for (const item of pending) item.resolve(new Response(JSON.stringify(response(100)), { status: 200 })); });
    expect(inputText()).toBe('—');
    view.rerender(<UsagePage projects={projects} models={models} timezone="UTC" />);
    await waitFor(() => expect(inputText()).toBe('900'));
    expect(newer).toHaveBeenCalledTimes(4);
    expect(pending.map(item => item.data.period)).toEqual(['month', 'day', 'month', 'all']);
  });

  it('retires an unmounted page without publishing into its replacement', async () => {
    const owner = heldBridge(); const first = render(<UsagePage projects={projects} models={models} timezone="UTC" />);
    const old = await batch(owner.pending); first.unmount();
    render(<UsagePage projects={projects} models={models} timezone="UTC" />);
    const current = await batch(owner.pending, 4);
    await answer(current, response(900, 0, 1)); await answer(old, response(100));
    expect(inputText()).toBe('900');
  });
});

describe('Usage presentation', () => {
  it('distinguishes pending and unavailable totals from an acknowledged zero result and retries', async () => {
    const owner = heldBridge(); render(<UsagePage projects={projects} models={models} timezone="UTC" />);
    const reads = await batch(owner.pending);
    expect(screen.getByText('Loading…')).toBeTruthy(); expect(screen.queryByText('No usage in this period')).toBeNull();
    expect(inputText()).toBe('—');
    await fail(reads, 'Synthetic accounting unavailable');
    expect(screen.getByRole('alert').textContent).toContain('Synthetic accounting unavailable');
    expect(screen.getByRole('heading', { name: 'Usage unavailable' })).toBeTruthy();
    expect(screen.queryByText('No usage in this period')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    await answer(await batch(owner.pending, 4), response());
    expect(screen.queryByRole('alert')).toBeNull(); expect(inputText()).toBe('0');
    expect(screen.getByRole('heading', { name: 'No usage in this period' })).toBeTruthy();
  });

  it('shows a previously acknowledged snapshot honestly during a failed same-scope refresh', async () => {
    const owner = heldBridge(); render(<UsagePage projects={projects} models={models} timezone="UTC" />);
    await answer(await batch(owner.pending), response(900, 0, 1));
    fireEvent.click(screen.getByRole('button', { name: 'Refresh usage' }));
    const reads = await batch(owner.pending, 4);
    expect(inputText()).toBe('900'); expect(screen.getByRole('status').textContent).toContain('Previously loaded');
    await fail(reads, 'Synthetic refresh unavailable');
    expect(inputText()).toBe('900'); expect(screen.getByText('Showing previously loaded totals. Refresh did not finish.')).toBeTruthy();
    expect(accounting().getAttribute('aria-busy')).toBe('false');
  });

  it('does not relabel old filter totals or provenance under a new scope', async () => {
    const owner = heldBridge(); render(<UsagePage projects={projects} models={models} timezone="UTC" />);
    await answer(await batch(owner.pending), response(900, 0, 1, { average_tps: 12, tracked_since: '2026-01-01', by_model: [{ model: 'old-provenance', input_tokens: 900, output_tokens: 0, requests: 1 }] }));
    expect(screen.getByText('old-provenance')).toBeTruthy();
    fireEvent.change(screen.getByRole('combobox', { name: 'Filter usage by model' }), { target: { value: 'fixture-b' } });
    await batch(owner.pending, 4);
    expect(inputText()).toBe('—'); expect(screen.queryByText('old-provenance')).toBeNull();
    expect(screen.queryByText('12.0 tok/s')).toBeNull(); expect(screen.queryByText(/Tracking since/)).toBeNull();
  });

  it('does not call nonzero recorded totals an absence of usage when daily entries are missing', async () => {
    const owner = heldBridge(); render(<UsagePage projects={projects} models={models} timezone="UTC" />);
    await answer(await batch(owner.pending), response(900, 0, 1, { average_tps: 12.345 }));
    expect(screen.getByRole('heading', { name: 'No daily breakdown' })).toBeTruthy();
    expect(screen.getByText('12.3 tok/s')).toBeTruthy(); expect(inputText()).toBe('900');
    expect(screen.queryByText('No usage in this period')).toBeNull();
  });

  it('keeps ordinary keyboard period buttons and a dismissible Refresh label without extra reads', async () => {
    const owner = heldBridge(); render(<UsagePage projects={projects} models={models} timezone="UTC" />);
    await answer(await batch(owner.pending), response());
    const group = screen.getByRole('group', { name: 'Usage period' });
    expect(within(group).getByRole('button', { name: 'Month' }).getAttribute('aria-pressed')).toBe('true');
    const refresh = screen.getByRole('button', { name: 'Refresh usage' });
    act(() => refresh.focus()); expect(document.activeElement).toBe(refresh);
    expect(refresh.querySelector('.control-label')?.textContent).toBe('Refresh usage');
    await userEvent.keyboard('{Escape}'); expect(refresh.querySelector('.control-label')).toBeNull();
    expect(owner.call).toHaveBeenCalledTimes(4);
    act(() => within(group).getByRole('button', { name: 'Day' }).focus());
    await userEvent.keyboard('{Enter}'); await batch(owner.pending, 4);
    expect(within(group).getByRole('button', { name: 'Day' }).getAttribute('aria-pressed')).toBe('true');
    expect(owner.call.mock.calls.slice(4).map(([, data]) => data.period)).toEqual(['day', 'day', 'month', 'all']);
  });
});
