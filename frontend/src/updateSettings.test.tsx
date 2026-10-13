import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { UpdateSettings } from './updateSettings';
import { UpdateHandoffSurface } from './updateHandoff';
import { updateReadiness } from './updateReadiness';
const attempt='a'.repeat(32);

const current = { current_version: '5.1.1', state: 'current', automatic_install_available: false,
  signing_setup_required: true, message: 'Unsigned fixture preview.', checked_at: 1_000_000 };

function bridge(status: unknown, checking: unknown = { ...current, state: 'checking' }, actions: Record<string, unknown> = {}) {
  const call = vi.fn(async (action: string, data: Record<string, unknown> = {}) => {
    if (action in actions) return typeof actions[action] === 'function' ? actions[action](data) : actions[action];
    if (action === 'host_status') return { visible: true, visibility_seq: 0 };
    if (action === 'update_status') return typeof status === 'function' ? status() : status;
    if (action === 'update_check') return checking;
    if (action === 'update_cancel') return { ok: true };
    throw new Error('Unexpected synthetic action: ' + action);
  });
  window.pywebview = { api: { call } };
  return call;
}
const statusCalls = (call: ReturnType<typeof bridge>) => call.mock.calls.filter(([action]) => action === 'update_status').length;

afterEach(() => { updateReadiness.aborted(attempt);updateReadiness.dismiss();vi.useRealTimers(); });

describe('update settings', () => {
  it('returns focus to the enabled Install trigger after confirmed prepared cancellation',async()=>{
    const ready={...current,state:'ready',automatic_install_available:true,safe_handoff_available:true,verified_download_available:true,verified:true,release:{version:'5.1.4',size:20,trust_mode:'ed25519'}};let finish!:(value:unknown)=>void;
    bridge(ready,undefined,{update_handoff_begin:{attempt_id:attempt,challenge:'b'.repeat(64)},update_handoff_ack:{phase:'prepared',confirmation:'c'.repeat(64)},update_handoff_commit:()=>new Promise(resolve=>{finish=resolve;}),update_handoff_abort:()=>{finish({attempt_id:attempt,blocked:true,phase:'failed'});return {attempt_id:attempt,aborted:true,not_executed:true};}});
    render(<><UpdateSettings notify={vi.fn()}/><UpdateHandoffSurface/></>);const button=await screen.findByRole('button',{name:'Install & restart'});button.focus();fireEvent.click(button);await screen.findByText('Preparing the verified restart. Forge will stay open until it is ready.');expect((button as HTMLButtonElement).disabled).toBe(true);
    await act(async()=>fireEvent.click(screen.getByRole('button',{name:'Keep working'})));await waitFor(()=>expect((button as HTMLButtonElement).disabled).toBe(false));expect(updateReadiness.canEdit()).toBe(true);expect(document.activeElement).toBe(button);
  });
  it('keeps user-selected focus after cancellation while a late commit response settles',async()=>{
    const ready={...current,state:'ready',automatic_install_available:true,safe_handoff_available:true,verified_download_available:true,verified:true,release:{version:'5.1.4',size:20,trust_mode:'ed25519'}};let finish!:(value:unknown)=>void;
    bridge(ready,undefined,{update_handoff_begin:{attempt_id:attempt,challenge:'b'.repeat(64)},update_handoff_ack:{phase:'prepared',confirmation:'c'.repeat(64)},update_handoff_commit:()=>new Promise(resolve=>{finish=resolve;}),update_handoff_abort:{attempt_id:attempt,aborted:true,not_executed:true}});
    render(<><UpdateSettings notify={vi.fn()}/><button>Continue elsewhere</button><UpdateHandoffSurface/></>);const button=await screen.findByRole('button',{name:'Install & restart'});button.focus();fireEvent.click(button);await screen.findByText('Preparing the verified restart. Forge will stay open until it is ready.');await act(async()=>fireEvent.click(screen.getByRole('button',{name:'Keep working'})));expect(updateReadiness.canEdit()).toBe(true);
    const other=screen.getByRole('button',{name:'Continue elsewhere'});other.focus();await act(async()=>finish({attempt_id:attempt,blocked:true,phase:'failed'}));await waitFor(()=>expect((button as HTMLButtonElement).disabled).toBe(false));expect(document.activeElement).toBe(other);
  });
  it('restores focus only after a lost abort reply is later confirmed over native status',async()=>{
    const ready={...current,state:'ready',automatic_install_available:true,safe_handoff_available:true,verified_download_available:true,verified:true,release:{version:'5.1.4',size:20,trust_mode:'ed25519'}};
    bridge(ready,undefined,{update_handoff_begin:{attempt_id:attempt,challenge:'b'.repeat(64)},update_handoff_ack:{phase:'prepared',confirmation:'c'.repeat(64)},update_handoff_commit:{attempt_id:attempt,blocked:true,phase:'failed',reason:'helper_failed'},update_handoff_abort:()=>Promise.reject(new Error('Synthetic lost reply')),update_handoff_status:{attempt_id:attempt,aborted:true,not_executed:true,phase:'failed'}});
    render(<><UpdateSettings notify={vi.fn()}/><UpdateHandoffSurface/></>);const button=await screen.findByRole('button',{name:'Install & restart'});button.focus();fireEvent.click(button);await screen.findByRole('button',{name:'Confirm cancellation'});await waitFor(()=>expect((button as HTMLButtonElement).disabled).toBe(false));expect(updateReadiness.canEdit()).toBe(false);expect(document.activeElement).not.toBe(button);
    await act(async()=>window.dispatchEvent(new CustomEvent('forge:update-handoff',{detail:{attempt_id:attempt}})));expect(updateReadiness.canEdit()).toBe(true);expect(document.activeElement).toBe(button);
  });
  it('offers explicit previous-version recovery only for a native authenticated completed operation',async()=>{
    const call=bridge({...current,state:'installed',safe_rollback_available:true,operation_id:'synthetic-completed-operation'},undefined,{update_handoff_begin:{blocked:true,reason:'native_input'}});render(<UpdateSettings notify={vi.fn()}/>);const button=await screen.findByRole('button',{name:'Restore previous version & restart',hidden:true});fireEvent.click(screen.getByText('Recovery'));fireEvent.click(button);
    await waitFor(()=>expect(call).toHaveBeenCalledWith('update_handoff_begin',{kind:'rollback'}));expect(call.mock.calls.some(([action])=>['update_apply','update_rollback'].includes(action))).toBe(false);expect(screen.getByText('Forge will confirm your saved work and verify the retained installation backup before restoring it. Your Forge data stays on this computer.')).toBeTruthy();
  });
  it.each([{}, {safe_rollback_available:true}])('keeps recovery closed without a completed authenticated native operation %j',async fields=>{
    bridge({...current,...fields});render(<UpdateSettings notify={vi.fn()}/>);await screen.findByText('Up to date');expect(screen.queryByRole('button',{name:'Restore previous version & restart',hidden:true})).toBeNull();
  });
  it('offers explicit restart only with a fresh native handoff capability and waits without duplicate starts', async () => {
    const ready={...current,state:'ready',automatic_install_available:true,safe_handoff_available:true,verified_download_available:true,verified:true,release:{version:'5.1.4',size:20,trust_mode:'ed25519'}};
    let answer!:(value:unknown)=>void;const call=bridge(ready,undefined,{update_handoff_begin:()=>new Promise(resolve=>{answer=resolve;})});render(<UpdateSettings notify={vi.fn()}/>);const button=await screen.findByRole('button',{name:'Install & restart'});fireEvent.click(button);fireEvent.click(button);
    await waitFor(()=>expect(call.mock.calls.filter(([action])=>action==='update_handoff_begin')).toHaveLength(1));expect(screen.getByRole('button',{name:'Checking saved work…'}).getAttribute('disabled')).not.toBeNull();await act(async()=>answer({blocked:true,reason:'native_input'}));expect(await screen.findByRole('button',{name:'Install & restart'})).toBeTruthy();
  });
  it('shows preference defaults without saving and only changes explicit controls', async () => {
    bridge(current); const change = vi.fn(async () => {});
    render(<UpdateSettings notify={vi.fn()} onChange={change} />);
    await screen.findByText('Up to date');
    expect(change).not.toHaveBeenCalled();
    expect(screen.getByRole('switch', { name: 'Check for updates automatically' }).getAttribute('aria-checked')).toBe('true');
    expect((screen.getByRole('combobox', { name: 'Update channel' }) as HTMLSelectElement).value).toBe('preview');
    fireEvent.click(screen.getByRole('switch', { name: 'Check for updates automatically' }));
    await waitFor(() => expect(change).toHaveBeenCalledWith({ update_checks: false }));
    fireEvent.change(screen.getByRole('combobox', { name: 'Update channel' }), { target: { value: 'stable' } });
    await waitFor(() => expect(change).toHaveBeenCalledWith({ update_channel: 'stable' }));
  });

  it('starts a non-blocking check on the selected channel and prevents duplicate clicks', async () => {
    const call = bridge(current);
    render(<UpdateSettings notify={vi.fn()} settings={{ update_channel: 'stable' }} onChange={vi.fn(async () => {})} />);
    await screen.findByText('Up to date');
    fireEvent.click(screen.getByRole('button', { name: 'Check for updates' }));
    await waitFor(() => expect(call).toHaveBeenCalledWith('update_check', { channel: 'stable', background: true }));
    expect(await screen.findByText('Checking…')).toBeTruthy();
    expect((screen.getByRole('button', { name: 'Check for updates' }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeTruthy();
  });

  it('offers a manual public download for an unsigned preview without an install action', async () => {
    bridge({ ...current, state: 'available', verified: true, release: { version: '5.1.2', prerelease: true, size: 1200,
      html_url: 'https://github.com/Bh0ps/forge-local-ai/releases/tag/v5.1.2',
      url: 'https://github.com/Bh0ps/forge-local-ai/releases/download/v5.1.2/Forge-5.1.2-Setup.exe' } });
    render(<UpdateSettings notify={vi.fn()} />);
    const download = await screen.findByRole('link', { name: 'Download installer' });
    expect(download.getAttribute('href')).toContain('/releases/download/v5.1.2/Forge-5.1.2-Setup.exe');
    expect(screen.getByText('Forge 5.1.2 preview is available.')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Install & restart' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Download & verify' })).toBeNull();
  });

  it('does not render a foreign installer link from malformed status', async () => {
    bridge({ ...current, state: 'available', release: { version: '5.1.2', size: 1200,
      html_url: 'https://github.com/Bh0ps/forge-local-ai/releases/tag/v5.1.2', url: 'https://example.invalid/installer.exe' } });
    render(<UpdateSettings notify={vi.fn()} />);
    expect((await screen.findByRole('link', { name: 'Download installer' })).getAttribute('href')).toBe('https://github.com/Bh0ps/forge-local-ai/releases/tag/v5.1.2');
  });

  it('does not keep polling an idle page', async () => {
    vi.useFakeTimers(); const call = bridge(current);
    render(<UpdateSettings notify={vi.fn()} />);
    await act(async () => { await Promise.resolve(); });
    expect(statusCalls(call)).toBe(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
    expect(statusCalls(call)).toBe(1);
  });

  it('polls a running check only while visible and stops after its honest failure state', async () => {
    vi.useFakeTimers(); let status = { ...current, state: 'checking', operation_error: '' };
    const call = bridge(() => status);
    render(<UpdateSettings notify={vi.fn()} />);
    await act(async () => { await Promise.resolve(); });
    await act(async () => { await vi.advanceTimersByTimeAsync(1500); });
    expect(statusCalls(call)).toBe(2);
    const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden');
    fireEvent(document, new Event('visibilitychange'));
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
    expect(statusCalls(call)).toBe(2);
    status = { ...status, state: 'error', operation_error: 'Synthetic connection unavailable.' };
    visibility.mockReturnValue('visible'); fireEvent(document, new Event('visibilitychange'));
    await act(async () => { await Promise.resolve(); });
    expect(screen.getByText('Check failed')).toBeTruthy();
    expect(screen.getByRole('alert').textContent).toContain('Synthetic connection unavailable.');
    const completedCalls = statusCalls(call);
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
    expect(statusCalls(call)).toBe(completedCalls);
  });

  it('also suspends polling when the native window is hidden', async () => {
    vi.useFakeTimers(); const call = bridge({ ...current, state: 'checking' });
    render(<UpdateSettings notify={vi.fn()} />);
    await act(async () => { await Promise.resolve(); });
    fireEvent(window, new CustomEvent('forge:visibility', { detail: { visible: false, seq: 1 } }));
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
    expect(statusCalls(call)).toBe(1);
    fireEvent(window, new CustomEvent('forge:visibility', { detail: { visible: true, seq: 2 } }));
    await act(async () => { await Promise.resolve(); await vi.advanceTimersByTimeAsync(1500); });
    expect(statusCalls(call)).toBe(3);
  });

  it('ignores a delayed initial response after a newer manual check starts', async () => {
    let resolve!: (value: unknown) => void;
    const pending = new Promise(done => { resolve = done; });
    const call = bridge(() => pending);
    render(<UpdateSettings notify={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: 'Check for updates' }));
    await waitFor(() => expect(call).toHaveBeenCalledWith('update_check', { channel: 'preview', background: true }));
    expect(await screen.findByText('Checking…')).toBeTruthy();
    await act(async () => { resolve(current); await pending; });
    expect(screen.getByText('Checking…')).toBeTruthy();
    expect(screen.queryByText('Up to date')).toBeNull();
  });

  const release = { version: '5.1.2', prerelease: true, size: 1200,
    html_url: 'https://github.com/Bh0ps/forge-local-ai/releases/tag/v5.1.2',
    url: 'https://github.com/Bh0ps/forge-local-ai/releases/download/v5.1.2/Forge-5.1.2-Setup.exe' };
  const publisherReady = { ...current, state: 'ready', automatic_install_available: true, signing_setup_required: false,
    publisher_proof_configured: true, verified: true, release: { ...release, trust_mode: 'ed25519' },
    signature: { mode: 'ed25519', proof_sha256: 'synthetic-digest', version: '5.1.2' } };

  it('distinguishes publisher-record verification from downloaded installer verification', async () => {
    const call = bridge({ ...publisherReady, state: 'available', verified: false, signature: undefined });
    render(<UpdateSettings notify={vi.fn()} />);
    expect(await screen.findByText('Publisher record verified')).toBeTruthy();
    expect(screen.getByText('Download to verify the installer against this publisher record.')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Download & verify' })).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Download installer' }).getAttribute('href')).toBe(release.url);
    expect(screen.queryByRole('button', { name: 'Install & restart' })).toBeNull();
    expect(screen.queryByText(/Windows publisher/)).toBeNull();
    expect(call.mock.calls.some(([action]) => action === 'update_apply')).toBe(false);
  });

  it('keeps publisher-verified seed downloads manual without claiming Windows signing or invoking install', async () => {
    const notify = vi.fn();
    const call = bridge(publisherReady, undefined, { update_apply: { ...publisherReady, prepared: true } });
    render(<UpdateSettings notify={notify} />);
    expect(await screen.findByText('Publisher verified installer')).toBeTruthy();
    expect(screen.getByText('The downloaded installer matches Forge’s authenticated release record.')).toBeTruthy();
    expect(screen.queryByText(/Windows/)).toBeNull();
    expect(screen.queryByText('synthetic-digest')).toBeNull();
    expect(call.mock.calls.some(([action]) => action === 'update_apply')).toBe(false);
    expect(screen.getByText('Verified download')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Install & restart' })).toBeNull();
    expect(screen.getByText('In-app installation is unavailable in this version. Save your work before installing manually.')).toBeTruthy();
    expect(notify).not.toHaveBeenCalled();
  });

  it('keeps valid legacy Windows certificate updates compatible', async () => {
    bridge({ ...publisherReady, publisher_proof_configured: false, release: { ...release, trust_mode: 'authenticode' },
      signature: { status: 'Valid', publisher: 'Synthetic publisher', version: '5.1.2', timestamped: true } });
    render(<UpdateSettings notify={vi.fn()} />);
    expect(await screen.findByText('Windows publisher verified')).toBeTruthy();
    expect(screen.getByText(/valid timestamped certificate/)).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Install & restart' })).toBeNull();
    expect(screen.getByRole('link', { name: 'Download installer' })).toBeTruthy();
    expect(screen.queryByText(/Publisher record verified/)).toBeNull();
  });

  it('keeps an unsigned legacy cache manual even when its cached state says ready', async () => {
    bridge({ ...current, state: 'ready', verified: true, release });
    render(<UpdateSettings notify={vi.fn()} />);
    expect(await screen.findByText('Manual install')).toBeTruthy();
    expect(screen.getByText('Manual installation')).toBeTruthy();
    expect(screen.queryByText('Ready to install')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Install & restart' })).toBeNull();
  });

  it('supports verified seed downloads independently of installation capability', async () => {
    const call = bridge({ ...publisherReady, state: 'available', verified: false, automatic_install_available: false, verified_download_available: true },
      undefined, { update_download: { ...publisherReady, state: 'downloading', verified: false, automatic_install_available: false, verified_download_available: true } });
    render(<UpdateSettings notify={vi.fn()} />);
    await screen.findByText('Publisher record verified');
    fireEvent.click(screen.getByRole('button', { name: 'Download & verify' }));
    await waitFor(() => expect(call).toHaveBeenCalledWith('update_download', {}));
    expect(screen.queryByRole('button', { name: 'Install & restart' })).toBeNull();
    expect(screen.getByRole('link', { name: 'Download installer' })).toBeTruthy();
  });

  it('does not offer restart or rollback even if an older cache advertises installation', async () => {
    const call = bridge({ ...publisherReady, operation_id: 'synthetic-old-operation' });
    render(<UpdateSettings notify={vi.fn()} />);
    await screen.findByText('Publisher verified installer');
    expect(screen.queryByRole('button', { name: 'Install & restart' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Roll back previous installation' })).toBeNull();
    expect(screen.getByText('In-app restore is unavailable in this version. Inspect retained backups before any manual repair.')).toBeTruthy();
    expect(call.mock.calls.some(([action]) => ['update_apply', 'update_rollback'].includes(action))).toBe(false);
  });

  it('does not label a mismatched staged publisher proof as a Windows certificate', async () => {
    bridge({ ...publisherReady, release: { ...release, trust_mode: 'authenticode' } });
    render(<UpdateSettings notify={vi.fn()} />);
    expect(await screen.findByText('Manual installation')).toBeTruthy();
    expect(screen.getByText('This download needs a fresh publisher verification. Check again or use the public installer.')).toBeTruthy();
    expect(screen.queryByText('Windows publisher verified')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Download & verify' })).toBeNull();
  });

  it.each([{ state: 'error', last_check_failed: false }, { state: 'available', last_check_failed: true }])
    ('requires a fresh check for stale cached proof status %j and restores trust after success', async failed => {
      const fresh = { ...publisherReady, state: 'available', verified: false, signature: undefined, last_check_failed: false };
      const call = bridge({ ...fresh, ...failed, operation_error: 'The cached publisher proof is unavailable.' }, fresh);
      render(<UpdateSettings notify={vi.fn()} />);
      expect(await screen.findByText('Manual installation')).toBeTruthy();
      expect(screen.getByText('Publisher verification needs another check. Retry the update check or use the public installer.')).toBeTruthy();
      expect(screen.queryByText('Publisher record verified')).toBeNull();
      expect(screen.queryByRole('button', { name: 'Download & verify' })).toBeNull();
      expect(screen.getByRole('link', { name: 'Download installer' }).getAttribute('href')).toBe(release.url);
      fireEvent.click(screen.getByRole('button', { name: 'Check for updates' }));
      expect(await screen.findByText('Publisher record verified')).toBeTruthy();
      expect(screen.getByRole('button', { name: 'Download & verify' })).toBeTruthy();
      expect(call).toHaveBeenCalledWith('update_check', { channel: 'preview', background: true });
    });

  it.each(['The publisher record is missing.', 'The publisher record expired.', 'The publisher record is invalid.', 'The publisher key is no longer trusted.'])
    ('explains manual-only verification once and disarms stale installation for %s', async reason => {
      bridge({ ...publisherReady, release: { ...release, trust_mode: 'manual', verification_error: reason } });
      render(<UpdateSettings notify={vi.fn()} />);
      expect(await screen.findByText('Manual installation')).toBeTruthy();
      expect(screen.getByText('Manual install')).toBeTruthy();
      expect(screen.queryByText('Ready to install')).toBeNull();
      expect(screen.getAllByText(reason)).toHaveLength(1);
      expect(screen.getByRole('link', { name: 'Download installer' }).getAttribute('href')).toBe(release.url);
      expect(screen.queryByRole('button', { name: 'Install & restart' })).toBeNull();
      expect(screen.queryByRole('button', { name: 'Download & verify' })).toBeNull();
      expect(screen.queryByText(/publisher verified/i)).toBeNull();
    });

  it('shows bounded progress and refreshes cancellation before allowing another update action', async () => {
    let status = { ...publisherReady, state: 'downloading', verified: false, received_bytes: 600 };
    const call = bridge(() => status, undefined, { update_cancel: () => { status = { ...status, state: 'cancelled' }; return { ok: true }; } });
    render(<UpdateSettings notify={vi.fn()} />);
    const progress = await screen.findByRole('progressbar', { name: 'Update download' }) as HTMLProgressElement;
    expect(progress.value).toBe(600); expect(progress.max).toBe(1200); expect(screen.getByText('50%')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(await screen.findByText('Cancelled')).toBeTruthy();
    expect(call.mock.calls.filter(([action]) => action === 'update_cancel')).toHaveLength(1);
    expect((screen.getByRole('button', { name: 'Check for updates' }) as HTMLButtonElement).disabled).toBe(false);
  });

  it('retries an honest failure by checking the selected channel', async () => {
    const call = bridge({ ...current, state: 'error', operation_error: 'Synthetic connection unavailable.' });
    render(<UpdateSettings notify={vi.fn()} settings={{ update_channel: 'stable' }} />);
    expect(await screen.findByRole('alert')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    await waitFor(() => expect(call).toHaveBeenCalledWith('update_check', { channel: 'stable', background: true }));
  });

  it('leaves a hidden running status static while keeping its honest text', async () => {
    bridge({ ...publisherReady, state: 'checking', verified: false });
    const { container } = render(<UpdateSettings notify={vi.fn()} />);
    await screen.findByText('Checking…');
    expect(container.querySelector('.update-status .spin')).toBeTruthy();
    fireEvent(window, new CustomEvent('forge:visibility', { detail: { visible: false, seq: 1 } }));
    expect(container.querySelector('.update-status .spin')).toBeNull();
    expect(screen.getByText('Checking…')).toBeTruthy();
  });
});
