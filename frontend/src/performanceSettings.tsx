import { CalibrationSettings } from './calibrationSettings';
import { useEffect, useRef, useState } from 'react';
import { Activity, Cpu, Gauge, HardDrive, Play, RefreshCw, Snowflake, Square, Thermometer, Zap } from 'lucide-react';
import { api, errorText, friendlyModel, tokenLabel } from './api';
import { Badge, ErrorNotice, Field, IconButton, Modal, Toggle } from './components';
import { ManagedRuntimes } from './runtimeSettings';
import { useWorkspaceVisibility } from './workspaceVisibility';
import { PerformanceTuning, type TuningContract, type TuningJob } from './performanceTuning';
import './performanceSettings.css';
import type { Model, Settings } from './types';

export interface PerformanceTelemetry {
  timestamp?: string;
  cpu?: { name?: string; physical_cores?: number; logical_cores?: number; utilization_percent?: number | null };
  ram?: { total_bytes?: number; available_bytes?: number; used_bytes?: number; percent_used?: number | null };
  gpus?: { name?: string; total_bytes?: number; used_bytes?: number; free_bytes?: number; utilization_percent?: number | null }[];
}
interface ResidentModel { name: string; size_bytes?: number; size_vram_bytes?: number; context_length?: number; expires_at?: string; }
interface Recommendation { id: string; title: string; description: string; reason?: string; settings: Partial<Settings>; requires_acceptance: true; evidence_kind?: 'measured' | 'hardware_estimate'; }
interface BenchmarkResult { case: string; status: string; ttft_seconds?: number; total_seconds?: number; input_tokens?: number; output_tokens?: number; tps?: number; estimated?: boolean; vision?: boolean; tools?: boolean; peak_gpu_bytes?: number; error?: string; }
interface BenchmarkJob extends TuningJob { selection?: Partial<Settings>; id: string; status: string; model?: string; provider_id?: string; context?: number; results?: BenchmarkResult[]; peak_gpu_bytes?: number; peak_ram_bytes?: number; summary?: string | { text?: string; [key: string]: unknown }; error?: string; purpose?: string; }
interface PerformanceData {
  tuning?: TuningContract;
  telemetry?: PerformanceTelemetry;
  selected?: { model: string; provider_id: string; context: number; thinking?: boolean; keep_alive?: string | number; num_thread?: number };
  engine?: { online: boolean; provider_id: string; residency_supported?: boolean; resident_models: ResidentModel[]; error?: string };
  recommendations?: Recommendation[]; benchmarks?: BenchmarkJob[];
  capabilities?: { benchmark?: boolean; warm_model?: boolean; release_model?: boolean };
  profiles?: { name?: string; id?: string; status?: string; validated?: boolean; description?: string }[]; notes?: string[];
}
interface Props { settings: Settings; models: Model[]; onChange: (delta: Partial<Settings>) => Promise<void>; onApply: (id: string) => Promise<void>; notify: (message: string) => void; refreshModels: () => Promise<void>; }
const activeStates = new Set(['queued', 'running', 'starting', 'warming', 'loading']);
export function memoryLabel(bytes?: number | null, unit: 'GiB' | 'GB' = 'GiB'): string { return typeof bytes === 'number' && Number.isFinite(bytes) && bytes >= 0 ? `${(bytes / (unit === 'GiB' ? 1073741824 : 1000000000)).toFixed(1)} ${unit}` : '—'; }
function percentLabel(value?: number | null) { return typeof value === 'number' && Number.isFinite(value) ? `${value.toFixed(0)}%` : '—'; }
function secondsLabel(value?: number) { return typeof value === 'number' && Number.isFinite(value) ? `${value.toFixed(2)} s` : '—'; }
function Meter({ value, label }: { value?: number | null; label: string }) { return <div className="performance-meter" role="meter" aria-label={label} aria-valuemin={0} aria-valuemax={100} aria-valuenow={value ?? undefined}><i style={{ width: `${Math.max(0, Math.min(100, value || 0))}%` }} /></div>; }
function settingLabel(key: string, value: unknown) { if (key === 'context') return `${tokenLabel(Number(value))} context`; if (key === 'model') return `Model: ${friendlyModel(String(value))}`; if (key === 'keep_alive') return value === 0 ? 'Release after request' : `Keep warm: ${String(value)}`; if (key === 'thinking') return `Reasoning ${value ? 'on' : 'off'}`; if (key === 'num_thread') return `${String(value)} CPU threads`; return `${key.replaceAll('_', ' ')}: ${String(value)}`; }
export function PerformanceSettings({ settings, models, onChange, onApply, notify, refreshModels }: Props) {
  const [data, setData] = useState<PerformanceData>({}); const [jobs, setJobs] = useState<BenchmarkJob[]>([]); const [error, setError] = useState(''); const [busy, setBusy] = useState('');
  const [recommendation, setRecommendation] = useState<Recommendation | null>(null); const [vision, setVision] = useState(true); const [tools, setTools] = useState(true);
  const visible = useWorkspaceVisibility(), bridge=window.pywebview?.api;
  const scope = JSON.stringify([settings.provider_id || 'ollama', settings.model, settings.context, settings.keep_alive, settings.thinking, settings.num_thread, settings.tokens, settings.temperature]);
  const owner = useRef({ scope, bridge: window.pywebview?.api, generation: 0, mounted: true });
  if (owner.current.scope !== scope || owner.current.bridge !== window.pywebview?.api) owner.current = { scope, bridge: window.pywebview?.api, generation: owner.current.generation + 1, mounted: true };
  const reading = useRef(-1), jobsReading = useRef(-1), dataSequence = useRef(0), jobsSequence = useRef(0), operation = useRef(false);
  const current = (generation: number) => owner.current.mounted && generation === owner.current.generation && owner.current.bridge === window.pywebview?.api;
  const scopedJobs = (items: BenchmarkJob[]) => items.filter(job => activeStates.has(job.status) || (!job.provider_id || job.provider_id === (settings.provider_id || 'ollama')) && (!job.model || job.model === settings.model) && (!job.context || job.context === settings.context) && (job.operation !== 'tune' || Boolean(job.selection && ['provider_id','model','context','thinking','tokens','temperature','num_thread','keep_alive'].every(key => job.selection?.[key] === (key === 'provider_id' ? settings.provider_id || 'ollama' : settings[key])))));
  async function refresh() {
    const generation = owner.current.generation;
    if (reading.current === generation) return;
    reading.current = generation; const request = ++dataSequence.current, jobsRequest = ++jobsSequence.current;
    try { const result = await api<PerformanceData>('performance'); if (!current(generation) || request !== dataSequence.current) return; setData(result); if (jobsRequest === jobsSequence.current) setJobs(scopedJobs(result.benchmarks || [])); setError(''); }
    catch (e) { if (current(generation) && request === dataSequence.current) setError(errorText(e)); }
    finally { if (reading.current === generation) reading.current = -1; }
  }
  async function refreshJobs() {
    const generation = owner.current.generation; if (jobsReading.current === generation) return;
    jobsReading.current = generation; const request = ++jobsSequence.current;
    try { const result = await api<{ benchmarks: BenchmarkJob[] }>('performance_benchmarks'); if (current(generation) && request === jobsSequence.current) setJobs(scopedJobs(result.benchmarks || [])); }
    catch (e) { if (current(generation) && request === jobsSequence.current) setError(errorText(e)); }
    finally { if (jobsReading.current === generation) jobsReading.current = -1; }
  }
  useEffect(() => { owner.current.mounted = true; return () => { owner.current.mounted = false; }; }, []);
  useEffect(() => { setData({}); setJobs([]); setRecommendation(null); setError(''); void refresh(); }, [scope,bridge]);
  useEffect(() => { if (!visible) return; const timer = setInterval(() => void refresh(), 5000); return () => clearInterval(timer); }, [scope, bridge, visible]);
  const active = jobs.some(job => activeStates.has(job.status));
  useEffect(() => { if (!active || !visible) return; const timer = setInterval(() => void refreshJobs(), 1000); return () => clearInterval(timer); }, [active, scope, bridge, visible]);
  async function action(name: string, model = settings.model) {
    if (operation.current) return; operation.current = true; const generation = owner.current.generation; setBusy(name); setError('');
    try {
      const result = await api<BenchmarkJob & { job?: BenchmarkJob }>(name, name === 'performance_tune' ? {} : { model, provider_id: settings.provider_id || 'ollama', context: settings.context, ...(name === 'performance_benchmark' ? { vision, tools } : {}) });
      if (!current(generation)) return; const job = result.job || result;
      if (job.id) setJobs(prev => [{ ...job, status: job.status || 'queued' }, ...prev.filter(item => item.id !== job.id)]);
      await refreshJobs(); if (!current(generation)) return;
      notify(name === 'performance_tune' ? 'Local tuning queued. Review the measured proposal before applying.' : name === 'performance_benchmark' ? 'Benchmark queued. Results include measured timings.' : name === 'performance_warm' ? 'Model warm-up queued.' : 'Model release queued.');
    } catch (e) { if (current(generation)) setError(errorText(e)); }
    finally { operation.current = false; setBusy(''); }
  }
  async function cancel(id: string) {
    const generation = owner.current.generation;
    try { await api('performance_cancel', { id }); if (current(generation)) await refreshJobs(); }
    catch (e) { if (current(generation)) setError(errorText(e)); }
  }
  async function approve(id: string) {
    if (operation.current) return; operation.current = true; const generation = owner.current.generation; setBusy('approve');
    try { const result = await api<{ settings: Settings }>('performance_profile_accept', { id }); if (!current(generation)) return; await onChange({ adaptive_profile_ids: result.settings.adaptive_profile_ids }); if (current(generation)) { await refresh(); notify('Measured profile approved. Apply it explicitly or enable approved adaptive tuning.'); } }
    catch (e) { if (current(generation)) setError(errorText(e)); }
    finally { operation.current = false; setBusy(''); }
  }
  async function apply() {
    if (!recommendation || operation.current) return; operation.current = true; const generation = owner.current.generation; setBusy('apply');
    try { await onApply(recommendation.id); if (current(generation)) { setRecommendation(null); await refresh(); notify('Performance recommendation applied.'); } }
    catch (e) { if (current(generation)) setError(errorText(e)); }
    finally { operation.current = false; setBusy(''); }
  }
  const ram = data.telemetry?.ram; const cpu = data.telemetry?.cpu; const gpus = data.telemetry?.gpus || [];
  const resident = data.engine?.resident_models?.find(model => model.name === settings.model || model.name === `${settings.model}:latest`);
  const selectedModel = models.find(model => model.name === settings.model);
  const residency = resident ? 'Loaded' : data.engine?.online === false ? 'Engine offline' : data.engine?.residency_supported ? 'Not loaded' : 'Residency unavailable';
  const tuning = data.tuning && (!data.tuning.selected || !['provider_id','model','context','thinking','tokens','temperature','num_thread','keep_alive'].every(key=>data.tuning?.selected?.[key]===(key==='provider_id'?settings.provider_id||'ollama':settings[key]))) ? {...data.tuning,eligible:false,reason:'identity_unavailable',phase:'waiting'} : data.tuning;
  return <div className="performance-page"><div className="settings-section-heading"><h2>Performance</h2><div className="performance-live"><span><i />{visible ? 'Live · 5 s' : 'Paused while hidden'}</span><IconButton label="Refresh performance" onClick={() => void refresh()}><RefreshCw size={15} /></IconButton></div></div>
    {error && <ErrorNotice error={error} retry={() => void refresh()} />}
    <PerformanceTuning settings={settings} tuning={tuning} recommendations={data.recommendations || []} jobs={jobs} busy={Boolean(busy)} residency={residency} measure={() => void action('performance_tune')} benchmark={data.capabilities?.benchmark ? () => void action('performance_benchmark') : undefined} cancel={id => void cancel(id)} review={setRecommendation} approve={id => void approve(id)} />
    <details className="performance-advanced"><summary><Gauge size={16} aria-hidden="true" /><span>Hardware & manual controls<small>Memory, runtime settings and detailed measurement history</small></span></summary>
    <section className="performance-hardware-grid">
      <article className="hardware-card"><header><HardDrive size={17} /><span>System RAM</span><Badge>{percentLabel(ram?.percent_used)} used</Badge></header><strong>{memoryLabel(ram?.available_bytes)}<small> available</small></strong><Meter value={ram?.percent_used} label="System RAM used" /><div className="hardware-values"><span>Used <b>{memoryLabel(ram?.used_bytes)}</b></span><span>Total <b>{memoryLabel(ram?.total_bytes)}</b></span></div><p>{ram?.total_bytes ? `${memoryLabel(ram.total_bytes, 'GB')} usable memory · GiB uses 1,024-based units` : 'Memory telemetry is unavailable.'}</p></article>
      <article className="hardware-card"><header><Cpu size={17} /><span>CPU</span><Badge>{percentLabel(cpu?.utilization_percent)}</Badge></header><strong className="hardware-name">{cpu?.name || 'Processor unavailable'}</strong><Meter value={cpu?.utilization_percent} label="CPU utilization" /><div className="hardware-values"><span>Physical cores <b>{cpu?.physical_cores ?? '—'}</b></span><span>Logical cores <b>{cpu?.logical_cores ?? '—'}</b></span></div><p>Live utilization across system processes.</p></article>
      {gpus.length ? gpus.map((gpu, index) => <article className="hardware-card gpu-card" key={`${gpu.name}-${index}`}><header><Gauge size={17} /><span>GPU {gpus.length > 1 ? index + 1 : ''}</span><Badge>{percentLabel(gpu.utilization_percent)}</Badge></header><strong className="hardware-name">{gpu.name || 'Graphics processor'}</strong><div className="gpu-free"><strong>{memoryLabel(gpu.free_bytes)}</strong><span>free VRAM</span></div><Meter value={gpu.total_bytes && gpu.used_bytes !== undefined ? gpu.used_bytes / gpu.total_bytes * 100 : undefined} label={`GPU ${index + 1} VRAM used`} /><div className="hardware-values"><span>Used <b>{memoryLabel(gpu.used_bytes)}</b></span><span>Total <b>{memoryLabel(gpu.total_bytes)}</b></span></div></article>) : <article className="hardware-card gpu-card"><header><Gauge size={17} /><span>GPU</span><Badge>Unavailable</Badge></header><p>No GPU telemetry is reported. CPU inference can still be available through your selected engine.</p></article>}
    </section>
    <div className="settings-section-heading"><h2>Selected model details</h2></div><section className="settings-card selected-model-card"><div className="selected-model-heading"><Zap size={22} /><div><strong>{settings.model ? friendlyModel(settings.model,selectedModel?.display_name) : 'Choose a model'}</strong><small>{settings.model || 'Select a local model in the composer.'}</small></div><Badge>{String(settings.provider_id || 'ollama')}</Badge></div><div className="model-footprint-grid"><div><span>Model file</span><strong>{memoryLabel(selectedModel?.size)}</strong><small>On-disk size</small></div><div><span>Resident footprint</span><strong>{memoryLabel(resident?.size_bytes)}</strong><small>Engine-reported allocation</small></div><div><span>GPU allocation</span><strong>{memoryLabel(resident?.size_vram_bytes)}</strong><small>Engine-reported VRAM</small></div><div><span>Selected context</span><strong>{tokenLabel(settings.context)}</strong><small>{resident?.context_length ? `${tokenLabel(resident.context_length)} loaded context` : 'Capacity depends on model and memory'}</small></div></div>{data.engine?.error && <p className="card-note">{data.engine.error}</p>}<div className="model-residency-controls"><button className="secondary" disabled={!settings.model || !data.capabilities?.warm_model || active || Boolean(busy)} onClick={() => void action('performance_warm')}><Thermometer size={14} />Load & keep warm</button><button className="subtle" disabled={!settings.model || !data.capabilities?.release_model || active || Boolean(busy)} onClick={() => void action('performance_release')}><Snowflake size={14} />Release model</button><span><Activity size={13} />One inference request at a time</span></div></section>
{Boolean(data.engine?.resident_models.length) && <details className="resident-models"><summary>Loaded models in this engine ({data.engine?.resident_models.length})</summary><div className="resident-model-list">{data.engine?.resident_models.map(item => <div key={item.name}><div><strong>{friendlyModel(item.name)}</strong><small>{item.name}</small></div><span>{memoryLabel(item.size_vram_bytes)} VRAM</span><span>{memoryLabel(item.size_bytes)} footprint</span><button className="text-button" disabled={!data.capabilities?.release_model || active || Boolean(busy)} onClick={() => void action('performance_release', item.name)}>Release</button></div>)}</div></details>}
    <h2>Inference controls</h2><section className="settings-card"><Toggle label="Keep model warm" description="Retain the selected model for 10 minutes between requests where supported." checked={settings.keep_alive !== 0} onChange={keep => void onChange({ keep_alive: keep ? '10m' : 0 })} /><div className="setting-row"><div><span>CPU inference threads</span><small>Used by Ollama. Other engines retain their runtime configuration.</small></div><input aria-label="CPU inference threads" className="performance-threads" type="number" min="1" max="64" value={Number(settings.num_thread) || cpu?.physical_cores || 4} onChange={event => { const value = Number(event.target.value); if (value >= 1 && value <= 64) void onChange({ num_thread: value }); }} /></div><Toggle label="Automatic delegation" description="Use enabled agent profiles within the shared goal budget." checked={Boolean(settings.auto_delegate)} onChange={value => void onChange({ auto_delegate: value })} /></section>
    <div className="settings-section-heading"><h2>Model benchmark</h2><Badge>Local measurements</Badge></div><section className="settings-card benchmark-settings"><p>Measure this model at your selected {tokenLabel(settings.context)} context. Probes use a small coding prompt, an optional image and a harmless tool call; they do not fill the entire context window.</p><div className="benchmark-options"><label><input type="checkbox" checked={vision} onChange={event => setVision(event.target.checked)} />Vision probe</label><label><input type="checkbox" checked={tools} onChange={event => setTools(event.target.checked)} />Tool-call probe</label><button className="primary" disabled={!settings.model || !data.capabilities?.benchmark || active || Boolean(busy)} onClick={() => void action('performance_benchmark')}><Play size={14} />{active ? 'Benchmark running…' : 'Run detailed benchmark'}</button></div>{jobs.slice(0, 5).map(job => <div className="benchmark-job" key={job.id}><header><div><strong>{job.model ? friendlyModel(job.model) : 'Model operation'}</strong><small>{job.context ? `${tokenLabel(job.context)} context` : ''}{job.provider_id ? ` · ${job.provider_id}` : ''}</small></div><Badge tone={job.status === 'completed' ? 'accent' : ''}>{job.status}</Badge>{activeStates.has(job.status) && <IconButton label="Cancel model operation" onClick={() => void cancel(job.id)}><Square size={13} /></IconButton>}</header>{job.results?.length ? <div className="benchmark-table-wrap"><table><thead><tr><th>Probe</th><th>First token</th><th>Total</th><th>Output</th><th>Decode speed</th></tr></thead><tbody>{job.results.map((result, index) => <tr key={`${result.case}-${index}`}><td><span>{result.case}</span><small>{result.status}{result.error ? ` · ${result.error}` : ''}</small></td><td>{secondsLabel(result.ttft_seconds)}</td><td>{secondsLabel(result.total_seconds)}</td><td>{result.output_tokens ?? '—'}{result.estimated && <small>estimated</small>}</td><td>{typeof result.tps === 'number' ? `${result.tps.toFixed(1)} tok/s` : '—'}{result.estimated && <small>estimated</small>}</td></tr>)}</tbody></table></div> : <p className="muted">{activeStates.has(job.status) ? 'Waiting for the engine. Cold model loading can take longer.' : job.error || 'No probe results.'}</p>}{(job.peak_gpu_bytes !== undefined || job.peak_ram_bytes !== undefined) && <div className="benchmark-peaks"><span>Peak GPU memory <strong>{memoryLabel(job.peak_gpu_bytes)}</strong></span><span>Peak system RAM <strong>{memoryLabel(job.peak_ram_bytes)}</strong></span><small>Measured across the complete benchmark</small></div>}{typeof job.summary === 'string' ? <p className="benchmark-summary">{job.summary}</p> : job.summary?.text ? <p className="benchmark-summary">{job.summary.text}</p> : null}</div>)}</section>
    <CalibrationSettings settings={settings} onChange={onChange} notify={notify}/><ManagedRuntimes settings={settings} notify={notify} onRefresh={async () => { await refresh(); await refreshModels(); }} />
    {data.profiles?.length ? <><h2>Engine profiles</h2><div className="profile-list">{data.profiles.map((profile, index) => <div key={profile.id || index}><strong>{profile.name || profile.id || 'Profile'}</strong><Badge>{profile.status || (profile.validated ? 'Validated' : 'Requires validation')}</Badge><p>{profile.description}</p></div>)}</div></> : null}
    {data.notes?.map((note, index) => <p key={index} className="page-footnote">{note}</p>)}
    </details>
    {recommendation && <Modal title="Apply performance recommendation?" close={() => { if (!busy) setRecommendation(null); }}><Badge>{recommendation.evidence_kind === 'measured' ? 'Measured evidence' : 'Hardware estimate'}</Badge><p>{recommendation.description}</p><div className="performance-apply-preview">{Object.entries(recommendation.settings).map(([key, value]) => <div key={key}>{settingLabel(key, value)}</div>)}</div>{recommendation.reason && <p className="muted">{recommendation.reason}</p>}<footer><button className="subtle" disabled={Boolean(busy)} onClick={() => setRecommendation(null)}>Cancel</button><button className="primary" disabled={Boolean(busy)} onClick={() => void apply()}>{busy === 'apply' ? 'Applying…' : 'Apply changes'}</button></footer></Modal>}
  </div>;
}
