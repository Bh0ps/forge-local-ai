import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { ArrowUpRight, Download, FileDown, History, LoaderCircle, PackageCheck, RefreshCw, ShieldAlert, ShieldCheck, X } from 'lucide-react';
import { api, errorText } from './api';
import { Badge, ErrorNotice, Field, Toggle } from './components';
import type { Settings } from './types';
import { useWorkspaceVisibility } from './workspaceVisibility';
import './updateSettings.css';
import { requestUpdateRestart } from './updateHandoff';
import { isNative } from './api';
import { updateReadiness, useUpdateReadiness } from './updateReadiness';

interface Update {
  state?: string; current_version: string; automatic_install_available: boolean; verified_download_available?: boolean; install_block_reason?: string;
  safe_handoff_available?: boolean; safe_rollback_available?: boolean;
  signing_setup_required: boolean; inspection_required?: boolean; publisher_proof_configured?: boolean; message: string;
  release?: { version: string; size: number; url?: string; html_url?: string; prerelease?: boolean;
    trust_mode?: 'ed25519' | 'authenticode' | 'manual'; verification_error?: string };
  signature?: { mode?: 'ed25519'; status?: string; publisher?: string; version?: string; timestamped?: boolean };
  verified?: boolean; error?: string; operation_error?: string; received_bytes?: number;
  operation_id?: string; checked_at?: number; last_check_failed?: boolean;
}
const releasesPage = 'https://github.com/Bh0ps/forge-local-ai/releases';
const stateLabels: Record<string, string> = { checking: 'Checking…', downloading: 'Downloading…', current: 'Up to date',
  available: 'Update available', ready: 'Verified download', prepared: 'Needs attention', installed: 'Updated',
  rolled_back: 'Restored', rollback_blocked: 'Needs attention', interrupted: 'Check needed', error: 'Check failed', cancelled: 'Cancelled' };

function publicLink(value: string | undefined, fallback: string) {
  return value && /^https:\/\/github\.com\/Bh0ps\/forge-local-ai\/releases\/(?:tag\/v?\d+\.\d+\.\d+|download\/v?\d+\.\d+\.\d+\/Forge-\d+\.\d+\.\d+-Setup\.exe)$/.test(value) ? value : fallback;
}

export function UpdateSettings({ notify, settings = {}, onChange }: {
  notify: (value: string) => void; settings?: Partial<Settings>; onChange?: (patch: Partial<Settings>) => Promise<void>;
}) {
  const [status, setStatus] = useState<Update>();
  const [busy, setBusy] = useState(false), [error, setError] = useState(''), [pendingAction, setPendingAction] = useState('');
  const [savingPreference, setSavingPreference] = useState(false), [exporting, setExporting] = useState(false);
  const [installing,setInstalling]=useState(false),installPending=useRef(false);
  const restartTrigger=useRef<HTMLButtonElement|null>(null),readiness=useUpdateReadiness();
  useLayoutEffect(()=>{
    if(installing || updateReadiness.isLocked() || !restartTrigger.current)return;
    const trigger=restartTrigger.current;restartTrigger.current=null;
    // The generic surface can unlock before this pending button is enabled.
    // Restore its focus after the render, unless the user chose another control.
    const focus=document.activeElement;
    if(trigger.isConnected && !trigger.disabled && (focus===document.body || focus instanceof Element && focus.closest('.update-handoff-surface')))trigger.focus();
  },[installing,readiness.phase]);
  const visible = useWorkspaceVisibility();
  const mounted = useRef(true), generation = useRef(0), actionPending = useRef(false), preferencePending = useRef(false), exportPending = useRef(false);
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
    if (actionPending.current || (active && name !== 'update_cancel')) return;
    actionPending.current = true;
    const request = ++generation.current;
    setBusy(true); setPendingAction(name); setError('');
    try {
      const next = await api<Update>(name,
        name === 'update_check' ? { channel, background: true } : {});
      if (!mounted.current || request !== generation.current) return;
      if (name === 'update_cancel') await refresh();
      else setStatus(next);
    } catch (exception) {
      if (mounted.current && request === generation.current) setError(errorText(exception));
    } finally {
      actionPending.current = false;
      if (mounted.current) { setBusy(false); setPendingAction(''); }
    }
  }
  async function preference(patch: Partial<Settings>) {
    if (preferencePending.current || !onChange) return;
    preferencePending.current = true; setSavingPreference(true); setError('');
    try { await onChange(patch); }
    catch (exception) { if (mounted.current) setError(errorText(exception)); }
    finally { preferencePending.current = false; if (mounted.current) setSavingPreference(false); }
  }
  async function diagnostics() {
    if (exportPending.current) return;
    exportPending.current = true; setExporting(true); setError('');
    try {
      const result = await api<{ diagnostics: unknown }>('diagnostics_export');
      if (!mounted.current) return;
      const blob = new Blob([JSON.stringify(result.diagnostics, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob), anchor = document.createElement('a');
      anchor.href = url; anchor.download = 'forge-diagnostics.json'; anchor.click(); URL.revokeObjectURL(url);
      notify('Diagnostics contain versions, counts and settings, with no conversations or private paths.');
    } catch (exception) { if (mounted.current) setError(errorText(exception)); }
    finally { exportPending.current = false; if (mounted.current) setExporting(false); }
  }
  async function restart(kind:'apply'|'rollback',trigger:HTMLButtonElement){if(installPending.current)return;restartTrigger.current=trigger;installPending.current=true;setInstalling(true);try{await requestUpdateRestart(kind);}finally{installPending.current=false;if(mounted.current)setInstalling(false);}}
  const release = status?.release;
  const releaseLink = publicLink(release?.html_url, releasesPage), downloadLink = publicLink(release?.url, releaseLink);
  const operationError = error || status?.operation_error || status?.error;
  const signatureMismatch = release?.trust_mode === 'authenticode' && status?.signature?.mode === 'ed25519' ||
    release?.trust_mode === 'ed25519' && status?.signature?.status === 'Valid';
  const freshCheckNeeded = Boolean(release && (status?.state === 'error' || status?.last_check_failed));
  const manual = release?.trust_mode === 'manual' || Boolean(release?.verification_error) || signatureMismatch || freshCheckNeeded;
  const canDownload = Boolean(release && (status?.verified_download_available ?? status?.automatic_install_available) && !manual && !status?.inspection_required);
  const canInstall=Boolean(isNative() && status?.safe_handoff_available===true && status?.automatic_install_available && status?.verified && canDownload && status.state==='ready');
  const canRestore=Boolean(isNative() && status?.safe_rollback_available===true && typeof status.operation_id==='string' && status.operation_id.length>0);
  const publisherProof = release?.trust_mode === 'ed25519' || (!release?.trust_mode && status?.signature?.mode === 'ed25519');
  const trusted = canDownload || release?.trust_mode === 'ed25519' || release?.trust_mode === 'authenticode';
  const verificationTitle = manual || !trusted ? 'Manual installation' : publisherProof ?
    status?.verified ? 'Publisher verified installer' : 'Publisher record verified' :
    status?.verified ? 'Windows publisher verified' : 'Windows publisher check';
  const verificationMessage = release?.verification_error || (signatureMismatch ? 'This download needs a fresh publisher verification. Check again or use the public installer.' : freshCheckNeeded ? 'Publisher verification needs another check. Retry the update check or use the public installer.' : manual || !trusted ?
    'A trusted publisher could not be verified. Download the installer to update Forge manually.' : publisherProof ?
    status?.verified ? 'The downloaded installer matches Forge’s authenticated release record.' : status?.state === 'downloading' ? 'The installer will be verified when the download finishes.' : 'Download to verify the installer against this publisher record.' :
    status?.verified ? 'The downloaded installer has a valid timestamped certificate from Forge’s approved publisher.' : 'The downloaded installer will be checked against Forge’s approved Windows publisher.');
  const stateLabel = pendingAction === 'update_cancel' ? 'Cancelling…' :
    pendingAction === 'update_check' ? 'Checking…' : pendingAction === 'update_download' ? 'Downloading…' :
    release && (manual || !trusted) && ['available', 'ready'].includes(status?.state || '') ? 'Manual install' : status ? stateLabels[status.state || ''] || 'Check needed' : error ? 'Unavailable' : 'Loading…';
  const animating = visible && (active || busy || !status && !error);
  const maximum = Math.max(1, release?.size || 1), received = Math.min(maximum, Math.max(0, status?.received_bytes || 0));
  const percent = Math.round(received / maximum * 100);

  return <>
    <h2>Updates</h2>
    <section className="settings-card update-settings-card" aria-busy={busy}>
      <div className="setting-row update-version"><span className="update-version-icon"><PackageCheck size={19} aria-hidden="true" /></span><div><strong>{status?.current_version ? `Forge ${status.current_version}` : 'Forge'}</strong>
        <small>{channel === 'preview' ? 'Preview channel' : 'Stable channel'}</small></div>
        <span className="update-status" role="status">{(active || busy || !status && !error) && <LoaderCircle size={12} aria-hidden="true" className={animating ? 'spin' : ''} />}<Badge>{stateLabel}</Badge></span></div>
      <Toggle label="Check for updates automatically" checked={settings.update_checks !== false} disabled={!onChange || savingPreference}
        description="Check GitHub in the background while the desktop app is running."
        onChange={enabled => void preference({ update_checks: enabled })} />
      <Field label="Update channel"><select value={channel} disabled={!onChange || savingPreference}
        onChange={event => void preference({ update_channel: event.target.value })}>
        <option value="preview">Preview — latest improvements</option><option value="stable">Stable releases</option>
      </select></Field>
      {release && <div className="update-release"><p>Forge {release.version}{release.prerelease ? ' preview' : ''} is available.</p>
        <div className="update-verification">{manual || !trusted ? <ShieldAlert size={17} aria-hidden="true" /> : <ShieldCheck size={17} aria-hidden="true" />}<div><strong>{verificationTitle}</strong><p>{verificationMessage}</p></div></div>
      </div>}
      {status?.state === 'downloading' && <div className="update-progress"><div><span>Downloading installer</span><span>{percent}%</span></div><progress aria-label="Update download" value={received} max={maximum} /></div>}
      {status?.inspection_required && <p className="muted">{status.message}</p>}
      {operationError && <ErrorNotice error={operationError} retry={!busy && !active ? () => void action('update_check') : undefined} />}
      <div className="setup-actions">
        <button type="button" className="secondary" disabled={busy || active} onClick={() => void action('update_check')}>
          <RefreshCw size={14} />Check for updates</button>
        {canDownload && !status?.verified && <button type="button" className="secondary" disabled={busy || active} onClick={() => void action('update_download')}>
          <Download size={14} />Download & verify</button>}
        {active && <button type="button" className="text-button" disabled={busy} onClick={() => void action('update_cancel')}><X size={13} />Cancel</button>}
        {canInstall && <button type="button" className="primary" disabled={busy || active || installing} onClick={event=>void restart('apply',event.currentTarget)}>{installing?'Checking saved work…':'Install & restart'}</button>}
      </div>
      {release && !manual && trusted && <small className="update-install-note">{canInstall ? 'Forge will confirm your saved work before restarting.' : 'In-app installation is unavailable in this version. Save your work before installing manually.'}</small>}
      <div className="update-links"><a href={releaseLink} target="_blank" rel="noopener noreferrer">Release notes <ArrowUpRight size={12} /></a>
        {release && <a href={downloadLink} target="_blank" rel="noopener noreferrer">Download installer <Download size={12} /></a>}</div>
      <small>{status?.checked_at ? `Last checked ${new Date(status.checked_at * 1000).toLocaleString()}` : 'No update check yet.'}</small>
      {status?.operation_id && <details><summary>Recovery</summary><p className="muted">{canRestore ? 'Forge will confirm your saved work and verify the retained installation backup before restoring it. Your Forge data stays on this computer.' : 'In-app restore is unavailable in this version. Inspect retained backups before any manual repair.'}</p>{canRestore && <button type="button" className="secondary" disabled={busy || active || installing} onClick={event=>void restart('rollback',event.currentTarget)}><History size={14}/>{installing?'Checking saved work…':'Restore previous version & restart'}</button>}</details>}
    </section>
    <h2>Diagnostics</h2>
    <section className="settings-card update-settings-card"><p className="muted">Share app and system versions, configuration and counts. Conversations, credentials and private paths stay on this computer.</p>
      <button type="button" className="secondary" disabled={exporting} onClick={() => void diagnostics()}><FileDown size={14} />{exporting ? 'Preparing report…' : 'Export diagnostic report'}</button></section>
  </>;
}
