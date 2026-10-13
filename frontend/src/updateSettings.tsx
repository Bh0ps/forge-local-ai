import { useEffect, useRef, useState } from 'react';
import { ArrowUpRight, Download, FileDown, RefreshCw, ShieldCheck } from 'lucide-react';
import { api, errorText, isNative } from './api';
import { Badge, ErrorNotice, Field, Toggle } from './components';
import type { Settings } from './types';
import { useWorkspaceVisibility } from './workspaceVisibility';
import './updateSettings.css';

interface Update {
  state?: string; current_version: string; automatic_install_available: boolean;
  signing_setup_required: boolean; inspection_required?: boolean; message: string;
  release?: { version: string; size: number; url?: string; html_url?: string; prerelease?: boolean };
  verified?: boolean; error?: string; operation_error?: string; received_bytes?: number;
  operation_id?: string; checked_at?: number;
}
const releasesPage = 'https://github.com/Bh0ps/forge-local-ai/releases';
const stateLabels: Record<string, string> = { checking: 'Checking…', downloading: 'Downloading…', current: 'Up to date',
  available: 'Update available', ready: 'Ready to install', prepared: 'Restarting…', installed: 'Updated',
  rolled_back: 'Restored', rollback_blocked: 'Needs attention', interrupted: 'Check needed', error: 'Check failed', cancelled: 'Cancelled' };

function publicLink(value: string | undefined, fallback: string) {
  return value && /^https:\/\/github\.com\/Bh0ps\/forge-local-ai\/releases\/(?:tag\/v?\d+\.\d+\.\d+|download\/v?\d+\.\d+\.\d+\/Forge-\d+\.\d+\.\d+-Setup\.exe)$/.test(value) ? value : fallback;
}

export function UpdateSettings({ notify, settings = {}, onChange }: {
  notify: (value: string) => void; settings?: Partial<Settings>; onChange?: (patch: Partial<Settings>) => Promise<void>;
}) {
  const [status, setStatus] = useState<Update>();
  const [busy, setBusy] = useState(false), [error, setError] = useState('');
  const visible = useWorkspaceVisibility();
  const mounted = useRef(true), generation = useRef(0);
  const channel = settings.update_channel === 'stable' ? 'stable' : 'preview';
  const active = status?.state === 'checking' || status?.state === 'downloading';

  async function refresh() {
    const request = ++generation.current;
    try {
      const next = await api<Update>('update_status');
      if (mounted.current && request === generation.current) { setStatus(next); setError(''); }
    } catch (exception) {
      if (mounted.current && request === generation.current) setError(errorText(exception));
    }
  }
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; generation.current++; }; }, []);
  useEffect(() => { if (visible && !busy) void refresh(); }, [visible]);
  useEffect(() => {
    if (!visible || !active || busy) return;
    const timer = setInterval(() => void refresh(), 1500);
    return () => clearInterval(timer);
  }, [visible, active, busy]);

  async function action(name: string) {
    const request = ++generation.current;
    setBusy(true); setError('');
    try {
      const next = await api<Update & { prepared?: boolean }>(name,
        name === 'update_check' ? { channel, background: true } : {});
      if (!mounted.current || request !== generation.current) return;
      if (next.prepared) notify('Forge is closing to apply the verified update. Your data backup is retained.');
      else if (name === 'update_cancel') await refresh();
      else setStatus(next);
    } catch (exception) {
      if (mounted.current && request === generation.current) setError(errorText(exception));
    } finally {
      if (mounted.current) setBusy(false);
    }
  }
  async function preference(patch: Partial<Settings>) {
    setError('');
    try { await onChange?.(patch); }
    catch (exception) { if (mounted.current) setError(errorText(exception)); }
  }
  async function diagnostics() {
    try {
      const result = await api<{ diagnostics: unknown }>('diagnostics_export');
      const blob = new Blob([JSON.stringify(result.diagnostics, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob), anchor = document.createElement('a');
      anchor.href = url; anchor.download = 'forge-diagnostics.json'; anchor.click(); URL.revokeObjectURL(url);
      notify('Diagnostics contain versions, counts and settings, with no conversations or private paths.');
    } catch (exception) { if (mounted.current) setError(errorText(exception)); }
  }
  const releaseLink = publicLink(status?.release?.html_url, releasesPage);
  const downloadLink = publicLink(status?.release?.url, releaseLink);
  const operationError = error || status?.operation_error || status?.error;

  return <>
    <h2>Updates</h2>
    <section className="settings-card update-settings-card">
      <div className="setting-row"><div><strong>Forge {status?.current_version || '5.1.1'}</strong>
        <small>{channel === 'preview' ? 'Preview channel' : 'Stable channel'}</small></div>
        <Badge>{stateLabels[status?.state || ''] || 'Ready'}</Badge></div>
      <Toggle label="Check for updates automatically" checked={settings.update_checks !== false} disabled={!onChange}
        description="Check GitHub in the background while the desktop app is running."
        onChange={enabled => void preference({ update_checks: enabled })} />
      <Field label="Update channel"><select value={channel} disabled={!onChange}
        onChange={event => void preference({ update_channel: event.target.value })}>
        <option value="preview">Preview — latest improvements</option><option value="stable">Stable releases</option>
      </select></Field>
      {status?.release && <p className="muted">Forge {status.release.version}{status.release.prerelease ? ' preview' : ''} is available.</p>}
      {status?.state === 'downloading' && <progress aria-label="Update download" value={status.received_bytes || 0} max={status.release?.size || 1} />}
      {status?.inspection_required && <p className="muted">{status.message}</p>}
      {status?.signing_setup_required && !status.inspection_required && <p className="muted">This unsigned preview requires a manual install.</p>}
      {operationError && <ErrorNotice error={operationError} />}
      <div className="setup-actions">
        <button type="button" className="secondary" disabled={busy || active} onClick={() => void action('update_check')}>
          <RefreshCw size={14} />Check for updates</button>
        {status?.release && (status.automatic_install_available ?
          <button type="button" className="secondary" disabled={busy || active || status.verified} onClick={() => void action('update_download')}>
            <Download size={14} />Download & verify</button> :
          <a className="secondary" href={downloadLink} target="_blank" rel="noopener noreferrer"><Download size={14} />Download installer</a>)}
        {status?.verified && status.automatic_install_available && <button type="button" className="primary" disabled={busy || active || !isNative()} onClick={() => void action('update_apply')}>
          <ShieldCheck size={14} />Install & restart</button>}
        {active && <button type="button" className="text-button" disabled={busy} onClick={() => void action('update_cancel')}>Cancel</button>}
      </div>
      <small>{status?.checked_at ? `Last checked ${new Date(status.checked_at * 1000).toLocaleString()}` : 'No update check yet.'}</small>
      <p><a href={releaseLink} target="_blank" rel="noopener noreferrer">Release notes <ArrowUpRight size={12} /></a></p>
      {status?.operation_id && <details><summary>Recovery</summary><p className="muted">Restore the saved installation when no newer work would be lost.</p>
        <button type="button" className="text-button" disabled={busy || active || !isNative() || status.inspection_required} onClick={() => void action('update_rollback')}>Roll back previous installation</button></details>}
    </section>
    <h2>Diagnostics</h2>
    <section className="settings-card update-settings-card"><p className="muted">Share app and system versions, configuration and counts. Conversations, credentials and private paths stay on this computer.</p>
      <button type="button" className="secondary" onClick={() => void diagnostics()}><FileDown size={14} />Export diagnostic report</button></section>
  </>;
}
