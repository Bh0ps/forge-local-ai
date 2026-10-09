import { useEffect, useRef, useState } from 'react';
import { Check, Download, LoaderCircle, Mic, RefreshCw, ShieldCheck, Sparkles } from 'lucide-react';
import { api, bytesLabel, errorText, friendlyModel, isNative, tokenLabel } from './api';
import { Badge, ErrorNotice, Field, Modal } from './components';
import type { Model, Settings } from './types';

interface Engine { id: string; name: string; online?: boolean; kind?: string; error?: string; }
interface Probe { case: string; status: string; tps?: number; ttft_seconds?: number; input_tokens?: number; repair?: string; evidence?: string; }
interface CatalogModel { name: string; title: string; size_bytes: number; description: string; fits_weights?: boolean; }
interface Scan {
  hardware?: { cpu: { name: string; physical_cores: number }; ram: { total_bytes: number }; gpus: { name: string; total_bytes: number; free_bytes?: number }[] };
  engines?: Engine[]; models?: Model[]; catalog?: CatalogModel[];
  recommendations?: { model: string; provider_id: string; size_bytes: number; memory_available_now: boolean; reason: string }[];
}
interface BrowserCheck { status?: string; verified?: boolean; evidence?: string; next_action?: string; }
interface Verification {
  results?: Probe[]; model?: string; context?: number; provider_id?: string;
  qualification?: { status: string; filled_context: boolean; input_tokens_measured?: number };
  dictation?: { model_installed: boolean; input_available: boolean; verified?: boolean };
}
interface Result extends Scan, Verification, BrowserCheck { requires_selection?: boolean; provider?: Engine; }
interface Job { id: string; operation: string; status: string; phase?: string; error?: string; result?: Result; results?: Probe[]; completed_bytes?: number; total_bytes?: number; }
interface Setup {
  completed: boolean; first_run: boolean; origin?: string; resume_available?: boolean;
  state: { scan?: Scan; verify?: Verification; install_engine?: Result; download_model?: Result; browser_verify?: BrowserCheck; dictation_check?: { verified: boolean; model?: string } };
  jobs: Job[]; catalog: CatalogModel[];
}
interface Speech { state: string; id?: string; text?: string; message?: string; error?: string; limit_reached?: boolean; }

export function SetupWizard({ settings, models, onChange, refreshModels, close, openSettings }: { settings: Settings; models: Model[]; onChange: (v: Partial<Settings>) => Promise<void>; refreshModels: () => Promise<void>; close: () => void; openSettings: (tab: string) => void }) {
  const [step, setStep] = useState(0); const [status, setStatus] = useState<Setup>();
  const [error, setError] = useState(''); const [busy, setBusy] = useState(false); const [engines, setEngines] = useState<Engine[]>([]);
  const [speech, setSpeech] = useState<Speech>({ state: 'idle' }); const [transcript, setTranscript] = useState(''); const [speechConfirmed, setSpeechConfirmed] = useState(false);
  const ownsMicrophone = useRef(false); const mounted = useRef(true); const speechJob = useRef('');
  const refreshPending=useRef(false);
  async function refresh() { if(refreshPending.current)return;refreshPending.current=true;try { const value = await api<Setup>('setup_status'); if (mounted.current) setStatus(value); } catch (e) { if (mounted.current) setError(errorText(e)); }finally{refreshPending.current=false;} }
  useEffect(() => {
    mounted.current = true; void refresh(); const timer = setInterval(() => void refresh(), 1200);
    return () => { mounted.current = false; clearInterval(timer); if (ownsMicrophone.current) { ownsMicrophone.current = false; void api('dictation_cancel').catch(() => {}); } };
  }, []);
  useEffect(() => { if (step !== 1) return; let live = true; void api<{ providers: Engine[] }>('providers').then(value => { if (live) setEngines(value.providers || []); }).catch(e => { if (live) setError(errorText(e)); }); return () => { live = false; }; }, [step, status?.state.install_engine?.provider_id]);

  const active = status?.jobs.find(j => ['queued', 'running', 'cancelling'].includes(j.status));
  const latest = (operation: string) => status?.jobs.find(j => j.operation === operation);
  const scan = latest('scan')?.status === 'completed' ? latest('scan')?.result : status?.state.scan;
  const verifyJob = latest('verify');
  const verification = verifyJob?.status === 'completed' ? verifyJob.result : status?.state.verify;
  const probes = verifyJob && ['running', 'cancelling', 'failed', 'interrupted'].includes(verifyJob.status) && verifyJob.results?.length ? verifyJob.results : verification?.results;
  const installed = latest('install_engine')?.status === 'completed' ? latest('install_engine')?.result : status?.state.install_engine;
  const downloaded = latest('download_model')?.status === 'completed' ? latest('download_model')?.result : status?.state.download_model;
  const browser = latest('browser_verify')?.status === 'completed' ? latest('browser_verify')?.result : status?.state.browser_verify;
  const failures = status?.jobs.filter((job, index, jobs) => ['failed', 'interrupted', 'cancelled'].includes(job.status) && jobs.findIndex(value => value.operation === job.operation) === index) || [];
  const providerId = String(settings.provider_id || 'ollama');
  const engineOptions = [...engines, ...(scan?.engines || []), ...(installed?.provider ? [installed.provider] : [])].filter((engine, index, values) => values.findIndex(value => value.id === engine.id) === index);
  if (!engineOptions.some(engine => engine.id === providerId)) engineOptions.unshift({ id: providerId, name: providerId === 'ollama' ? 'Ollama' : providerId });
  const microphoneActive = ['starting', 'recording', 'stopping', 'transcribing'].includes(speech.state);
  const locked = busy || Boolean(active) || microphoneActive;

  async function run(action: string, data: Record<string, unknown> = {}) { setError(''); setBusy(true); try { await api(action, data); await refresh(); } catch (e) { setError(errorText(e)); } finally { setBusy(false); } }
  async function choose(delta: Partial<Settings>) { setError(''); setBusy(true); try { await onChange(delta); if ('provider_id' in delta || 'model' in delta) await refreshModels(); } catch (e) { setError(errorText(e)); } finally { setBusy(false); } }
  async function finish(skip = false) { setError(''); setBusy(true); try { if (ownsMicrophone.current) { await api('dictation_cancel'); ownsMicrophone.current = false; } await api(skip ? 'setup_skip' : 'setup_complete'); await refreshModels(); close(); } catch (e) { setError(errorText(e)); } finally { setBusy(false); } }
  function receiveSpeech(value: Speech) {
    if (value.state === 'error') throw new Error(value.message || value.error || 'Microphone test failed. Check Dictation settings.');
    if (!['starting', 'recording', 'stopping', 'transcribing', 'done', 'cancelled'].includes(value.state)) throw new Error(value.message || 'The microphone is not ready. Prepare the speech model in Dictation settings.');
    if (value.id) speechJob.current = value.id;
    setSpeech(value);
    ownsMicrophone.current = ['starting', 'recording', 'stopping', 'transcribing'].includes(value.state);
    if (value.state === 'done') { setTranscript(value.text || ''); if (!value.text?.trim()) setError('No speech detected. Try again and check your microphone.'); }
  }
  async function microphone(action: 'dictation_start' | 'dictation_stop' | 'dictation_cancel') {
    setError(''); setBusy(true);
    try {
      if (action === 'dictation_start') { ownsMicrophone.current = true; setTranscript(''); setSpeechConfirmed(false); speechJob.current = ''; }
      const value = await api<Speech>(action);
      if (!mounted.current) { if (['starting', 'recording', 'stopping', 'transcribing'].includes(value.state)) await api('dictation_cancel'); return; }
      receiveSpeech(value);
    }
    catch (e) { if (ownsMicrophone.current) { ownsMicrophone.current = false; void api('dictation_cancel').catch(() => {}); } setError(errorText(e)); setSpeech({ state: 'error' }); }
    finally { setBusy(false); }
  }
  useEffect(() => {
    if (!microphoneActive) return; let fetching = false; let live = true;
    const timer = setInterval(async () => {
      if (fetching) return; fetching = true;
      try {
        const value = await api<Speech>('dictation_status');
        if (!live || value.id && speechJob.current && value.id !== speechJob.current) return;
        if (value.limit_reached && value.state === 'recording') receiveSpeech(await api<Speech>('dictation_stop'));
        else receiveSpeech(value);
      } catch (e) { if (live) { setError(errorText(e)); setSpeech({ state: 'error' }); } }
      finally { fetching = false; }
    }, 600);
    return () => { live = false; clearInterval(timer); };
  }, [microphoneActive]);
  async function confirmSpeech() {
    setError(''); setBusy(true);
    try { await api('setup_dictation_confirm', { id: speech.id, accepted: true }); setSpeechConfirmed(true); await refresh(); }
    catch (e) { setError(errorText(e)); } finally { setBusy(false); }
  }
  const dictationVerified = speechConfirmed || Boolean(status?.state.dictation_check?.verified && status.state.dictation_check.model === String(settings.dictation_model || 'base'));

  return <Modal title="Set up your Forge workspace" close={close} wide>
    <div className="setup-steps">{['Your computer', 'Engine & model', 'Permissions', 'Verify'].map((name, i) => <button key={name} className={step === i ? 'active' : ''} onClick={() => setStep(i)}><span>{i + 1}</span>{name}</button>)}</div>
    {status?.origin === 'upgraded' && <p className="muted">Your existing workspace is connected. These checks use your saved selections.</p>}
    {status?.resume_available && <p className="muted">Saved setup progress is available. Retry any interrupted step below.</p>}
    {error && <ErrorNotice error={error} />}
    {active && <div className="setup-progress" role="status"><LoaderCircle size={16} className="spin" /><span>{active.phase || active.operation.replaceAll('_', ' ')}{active.total_bytes ? ` · ${bytesLabel(active.completed_bytes)} / ${bytesLabel(active.total_bytes)}` : ''}</span><button className="text-button" disabled={busy || active.status === 'cancelling'} onClick={() => void run('setup_cancel', { id: active.id })}>{active.status === 'cancelling' ? 'Cancelling…' : 'Cancel'}</button></div>}
    {failures.map(job => <div key={job.id}><ErrorNotice error={`${job.operation.replaceAll('_', ' ')}: ${job.error || `Step ${job.status}.`}`} /><button className="text-button" disabled={locked} onClick={() => void run('setup_retry', { id: job.id })}>Retry {job.operation.replaceAll('_', ' ')}</button></div>)}
    <div className="setup-body">
      {step === 0 && <><h3>Built around your hardware</h3><p className="muted">Scan connected engines and available memory. Your saved model and context stay selected until you choose a change.</p><button className="secondary" disabled={locked} onClick={() => void run('setup_scan')}><RefreshCw size={14} />Detect my computer</button>{scan?.hardware && <div className="setup-hardware"><strong>{scan.hardware.cpu.name}</strong><span>{scan.hardware.cpu.physical_cores} CPU cores · {bytesLabel(scan.hardware.ram.total_bytes)} RAM</span>{scan.hardware.gpus.map(g => <span key={g.name}>{g.name} · {bytesLabel(g.total_bytes)} GPU memory{g.free_bytes !== undefined ? ` · ${bytesLabel(g.free_bytes)} free` : ''}</span>)}</div>}{scan?.engines?.map(engine => <div className="setting-row" key={engine.id}><span>{engine.name}</span><Badge tone={engine.online ? 'success' : ''}>{engine.online ? 'Connected' : 'Offline'}</Badge></div>)}</>}
      {step === 1 && <><h3>Choose a local engine and model</h3>
        <Field label="Engine"><select value={providerId} disabled={locked} onChange={e => void choose({ provider_id: e.target.value, model: '' })}>{engineOptions.map(engine => <option value={engine.id} key={engine.id}>{engine.name}{engine.online === false ? ' (offline)' : ''}</option>)}</select></Field>
        {installed?.provider_id && installed.provider_id !== providerId && <div className="setup-model"><div><strong>{installed.provider?.name || 'Managed Ollama'} is installed</strong><p>Select this engine to download and use its models.</p></div><button className="secondary" disabled={locked} onClick={() => void choose({ provider_id: installed.provider_id, model: '' })}>Use installed engine</button></div>}
        <Field label="Installed model"><select value={settings.model} disabled={locked} onChange={e => void choose({ model: e.target.value })}><option value="">Choose a model</option>{models.map(model => <option key={model.name}>{model.name}</option>)}</select></Field><p className="muted">{friendlyModel(settings.model || 'No model selected')} · {tokenLabel(settings.context)} context</p>
        {scan?.recommendations?.map(model => <div className="setup-model" key={`${model.provider_id}/${model.model}`}><div><strong>Installed recommendation: {friendlyModel(model.model)}</strong><small>{bytesLabel(model.size_bytes)} weights · tools and vision advertised</small><p>{model.memory_available_now ? 'Estimated memory is available now.' : 'Available memory is currently limited; close other loaded models before verification.'} Context capacity needs verification.</p></div><button className="secondary" disabled={locked} onClick={() => void choose({ provider_id: model.provider_id, model: model.model })}>Use recommended model</button></div>)}
        {downloaded?.model && (downloaded.model !== settings.model || downloaded.provider_id !== providerId) && <div className="setup-model"><div><strong>{friendlyModel(downloaded.model)} is downloaded</strong><p>Ready to select and verify.</p></div><button className="secondary" disabled={locked} onClick={() => void choose({ model: downloaded.model, provider_id: downloaded.provider_id })}>Use downloaded model</button></div>}
        <div className="setup-models">{(scan?.catalog || status?.catalog || []).map(model => <div className="setup-model" key={model.name}><div><strong>{model.title}</strong><small>{model.name} · up to {bytesLabel(model.size_bytes)}</small><p>{model.description}{model.fits_weights === false ? ' Model weights may exceed this computer’s available capacity.' : ''}</p></div><button className="secondary" disabled={locked} onClick={() => void run('setup_download_model', { model: model.name })}><Download size={14} />Download</button></div>)}</div>
        <p className="muted">Catalog downloads require a selected Ollama engine. Guided installation creates a managed engine; select it when installation completes.</p>
        <button className="text-button" disabled={locked} onClick={() => void run('setup_install_engine', { engine: 'ollama' })}>Install verified managed Ollama</button><button className="text-button" disabled={locked} onClick={() => void refreshModels().catch(e => setError(errorText(e)))}>Refresh installed models</button><button className="text-button" onClick={() => openSettings('models')}>Connect another engine or use Hugging Face</button>
      </>}
      {step === 2 && <><h3>You control tool access</h3><div className="permission-options">{[{ id: 'always_ask', title: 'Always Ask', text: 'Read connected projects. Ask before edits, commands or computer actions.' }, { id: 'full_access', title: 'Full Access', text: 'Run enabled tools within configured scopes.' }, { id: 'deny_access', title: 'Chat only', text: 'Disable tools and keep conversations available.' }].map(permission => <button key={permission.id} disabled={busy} className={settings.permission_profile === permission.id ? 'active' : ''} onClick={() => void choose({ permission_profile: permission.id as Settings['permission_profile'] })}><div><ShieldCheck size={16} /><strong>{permission.title}</strong></div><p>{permission.text}</p></button>)}</div><p className="muted">Memory saves require review. Telegram accounts must be paired. GitHub pull requests require specific approval. These integrations cannot expand tool permissions.</p></>}
      {step === 3 && <><section className="settings-card"><h3>OpenRouter goal guidance</h3><p className="muted">Set up scoped planning and independent review, then verify the actual goal workflow. Account authentication alone does not prove these stages.</p><button className="secondary" onClick={()=>openSettings('openrouter')}>Open goal connections</button></section><h3>Check the whole workflow</h3><p className="muted">Run short coding, tool and vision checks using your selected model and context. Larger models can take up to an hour to produce their first response.</p><button className="primary" disabled={locked || !settings.model} onClick={() => void run('setup_verify')}><Sparkles size={15} />Verify model</button>
        {verification?.model && <p className="muted">Measured with {friendlyModel(verification.model)} · {tokenLabel(verification.context || settings.context)} context{verification.model !== settings.model || verification.provider_id !== providerId || verification.context !== settings.context ? ' · Selection changed; rerun verification.' : ''}</p>}
        {probes?.map(probe => <div key={probe.case}><div className="setting-row"><span>{probe.case.replaceAll('_', ' ')}</span><Badge tone={probe.status === 'passed' ? 'success' : ''}>{probe.status}{probe.tps !== undefined ? ` · ${probe.tps.toFixed(1)} tokens/s` : ''}</Badge></div>{probe.repair && <p className="muted">{probe.repair}</p>}{probe.case === 'context' && probe.input_tokens !== undefined && <p className="muted">{probe.input_tokens.toLocaleString()} input tokens measured in the bounded context probe.</p>}</div>)}
        <div className="setting-row"><span>Local dictation {dictationVerified && <Badge tone="success">Verified</Badge>}</span><button className="text-button" onClick={() => openSettings('dictation')}>{verification?.dictation?.model_installed ? 'Speech model settings' : 'Install speech model'}</button></div>
        <p className="muted">Click Test microphone, say a short phrase, then stop and review the transcript. The test does not send a chat message.</p>
        {!isNative() && <p className="muted">Open Forge desktop for this microphone check.</p>}
        <button className="secondary" disabled={busy || Boolean(active) || !isNative() || ['starting', 'stopping', 'transcribing'].includes(speech.state)} onClick={() => void microphone(speech.state === 'recording' ? 'dictation_stop' : 'dictation_start')}><Mic size={14} />{speech.state === 'recording' ? 'Stop microphone test' : speech.state === 'transcribing' ? 'Transcribing…' : speech.state === 'starting' ? 'Preparing microphone…' : 'Test microphone'}</button>
        {microphoneActive && <button className="text-button" disabled={busy} onClick={() => void microphone('dictation_cancel')}>Cancel microphone test</button>}
        {speech.state === 'done' && speech.text?.trim() && <><Field label="Test transcript" hint="Edit locally before confirming. Setup retains only the verification result."><textarea value={transcript} onChange={event => setTranscript(event.target.value)} /></Field><button className="secondary" disabled={busy || speechConfirmed || !speech.id || !transcript.trim()} onClick={() => void confirmSpeech()}>{speechConfirmed ? 'Transcript confirmed' : 'Confirm microphone transcript'}</button></>}
        <div className="setting-row"><span>Browser navigation {browser?.status && <Badge tone={browser.verified ? 'success' : ''}>{browser.status}</Badge>}</span><button className="text-button" disabled={locked} onClick={() => void run('setup_browser_verify')}>Test browser</button></div>{browser?.next_action && <p className="muted">{browser.next_action}</p>}{browser?.evidence && <p className="muted">{browser.evidence}</p>}<button className="text-button" onClick={() => openSettings('browser')}>Open browser settings</button>
        <p className="muted">The browser check uses a disposable local page. Short model probes do not qualify the full context window or every tool. Failed checks remain visible and can be rerun.</p>
      </>}
    </div>
    <footer><button className="subtle" disabled={busy} onClick={() => void finish(true)}>Skip for now</button>{step > 0 && <button className="secondary" onClick={() => setStep(step - 1)}>Back</button>}{step < 3 ? <button className="primary" onClick={() => setStep(step + 1)}>Continue</button> : <button className="primary" disabled={locked || !settings.model} onClick={() => void finish()}><Check size={14} />Finish setup</button>}</footer>
  </Modal>;
}
