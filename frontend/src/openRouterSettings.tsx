import { useEffect, useState } from 'react';
import { Bot, Check, KeyRound, ShieldCheck, Sparkles } from 'lucide-react';
import { api, errorText, tokenLabel } from './api';
import { Badge, ErrorNotice, Field, Toggle } from './components';
import type { Agent, Settings } from './types';
import './openRouter.css';

interface Provider {
  enabled?: boolean; connected?: boolean; remote_consent?: boolean; data_collection?: string;
  account?: { free_model_daily_requests?: { used: number; limit: number; remaining: number }; quota_note?: string };
  models?: { name: string }[];
}
interface WorkflowStatus {
  guidance_enabled?: boolean;
  configuration?: {state:string}; authentication?: {state:string;checked_at?:string;error?:string};
  free_capacity?: {state:string;remaining?:number;checked_at?:string;note?:string}; catalog?: {state:string;free_models?:number;checked_at?:string};
  profiles?: {state:string;missing?:string[];ready?:string[];checked_at?:string};
  workflow?: {state:string;checked_at?:string;receipt_id?:string;checks?:unknown[]};
}
const FREE_ROUTER = 'openrouter/free';
const connectionOptions = (provider: Provider) => ({ enabled: Boolean(provider.enabled), remote_consent: Boolean(provider.remote_consent), data_collection: provider.data_collection || 'deny' });

export function OpenRouterSettings({ notify, settings, onChange }: { notify: (v: string) => void; settings?: Settings; onChange?: (v: Partial<Settings>) => Promise<void> }) {
  const [provider, setProvider] = useState<Provider>({});
  const [savedProvider, setSavedProvider] = useState<Provider>({});
  const [key, setKey] = useState(''); const [error, setError] = useState(''); const [busy, setBusy] = useState(false);
  const [workflow,setWorkflow]=useState<WorkflowStatus|null>(null), [selfTest,setSelfTest]=useState<{id:string;state:string;stages?:unknown[];failure_reason?:string}|null>(null);
  async function refreshWorkflow(){const value=await api<WorkflowStatus>('ai_workflow_status');setWorkflow(value);}
  useEffect(() => {
    let disposed = false;
    void api<{ provider: Provider }>('openrouter_status').then(result => { if (!disposed) { setProvider(result.provider || {}); setSavedProvider(result.provider || {}); } }).catch(e => { if (!disposed) setError(errorText(e)); });
    void refreshWorkflow().catch(()=>{});
    return () => { disposed = true; };
  }, []);
  useEffect(()=>{if(!selfTest||selfTest.state!=='running')return;let alive=true,pending=false;const timer=setInterval(async()=>{if(pending||document.visibilityState==='hidden')return;pending=true;try{const value=await api<typeof selfTest>('ai_workflow_self_test_status',{id:selfTest.id});if(alive){setSelfTest(value);if(value?.state!=='running')await refreshWorkflow();}}catch(e){if(alive)setError(errorText(e));}finally{pending=false;}},1500);return()=>{alive=false;clearInterval(timer);};},[selfTest?.id,selfTest?.state]);
  const ready = Boolean(savedProvider.enabled && savedProvider.connected && savedProvider.remote_consent && !key && JSON.stringify(connectionOptions(provider)) === JSON.stringify(connectionOptions(savedProvider)));
  const quota = savedProvider.account?.free_model_daily_requests;
  const reviewModel = String(settings?.goal_review_model || FREE_ROUTER);
  const reviewContext = Number(settings?.goal_review_context || 32768);
  const reviewContexts = [...new Set([8192,16384,32768,65536,reviewContext])].sort((a,b) => a-b);
  const models = [...new Set([FREE_ROUTER, reviewModel, ...(savedProvider.models || []).map(model => model.name)])];
  async function change(delta: Partial<Settings>) {
    if (!onChange) return;
    setError(''); setBusy(true);
    try { await onChange(delta); } catch (e) { setError(errorText(e)); } finally { setBusy(false); }
  }
  async function save(test = false) {
    setBusy(true); setError('');
    try {
      const saved = await api<{ provider: Provider }>('openrouter_save', { ...connectionOptions(provider), ...(key ? { api_key: key } : {}) });
      setKey(''); setProvider(saved.provider); setSavedProvider(saved.provider);
      if (test) { const checked = await api<{ provider: Provider }>('openrouter_test'); setProvider(checked.provider); setSavedProvider(checked.provider); }
      notify(test ? 'OpenRouter connection tested.' : 'OpenRouter connection saved.');
      await refreshWorkflow().catch(()=>{});
    } catch (e) { setError(errorText(e)); } finally { setBusy(false); }
  }
  async function setupAgents() {
    setBusy(true); setError('');
    try {
      const result = await api<{ agents: Agent[]; settings?: Partial<Settings>; provider?: Provider }>('openrouter_setup_agents');
      if (result.provider) { setProvider(result.provider); setSavedProvider(result.provider); }
      if (onChange && result.settings) await onChange({ auto_delegate: Boolean(result.settings.auto_delegate), goal_review_enabled: Boolean(result.settings.goal_review_enabled), goal_review_model: String(result.settings.goal_review_model || FREE_ROUTER), goal_review_max_revisions: Number(result.settings.goal_review_max_revisions || 3), goal_review_context: Number(result.settings.goal_review_context || 32768) });
      notify('OpenRouter helpers and independent goal review are ready. Your main model stays selected.');
    } catch (e) { setError(errorText(e)); } finally { setBusy(false); }
  }
  async function setupSpecialists() {
    setBusy(true);setError('');
    try {const result=await api<{settings:Partial<Settings>}>('openrouter_setup_specialists');if(onChange)await onChange({auto_delegate:true,goal_review_enabled:Boolean(result.settings.goal_review_enabled)});notify('Free planners and specialists are ready. Builder can continue locally when advisory cloud help is unavailable.');}catch(e){setError(errorText(e));}finally{setBusy(false);}
  }
  async function setupWorkflow(){setBusy(true);setError('');try{const result=await api<{status:WorkflowStatus;settings?:Partial<Settings>}>('ai_workflow_setup',{goal_guidance:true});setWorkflow(result.status);if(onChange&&result.settings)await onChange({goal_cloud_guidance:Boolean(result.settings.goal_cloud_guidance),auto_delegate:Boolean(result.settings.auto_delegate),goal_review_enabled:Boolean(result.settings.goal_review_enabled)});notify('Goal guidance and free cloud workflow profiles configured. Run the workflow check to verify execution.');}catch(e){setError(errorText(e));}finally{setBusy(false);}}
  async function testWorkflow(){setBusy(true);setError('');try{const result=await api<{id:string;state:string;stages?:unknown[];failure_reason?:string}>('ai_workflow_self_test');setSelfTest(result);}catch(e){setError(errorText(e));}finally{setBusy(false);}}
  return <div className="openrouter-settings">
    <div className="openrouter-title"><div><h2>OpenRouter team</h2><p className="muted">Give your local agent remote help and an independent completion check.</p></div><Badge>{workflow?.workflow?.state==='verified' ? 'Workflow verified' : savedProvider.connected ? 'Account authenticated' : 'Setup needed'}</Badge></div>
    {error && <ErrorNotice error={error} />}
    <section className="settings-card">
      <div className="openrouter-section-title"><KeyRound size={17} /><div><h3>Connection</h3><small>Free-only models · No additional local VRAM</small></div></div>
      <Field label="OpenRouter API key" hint="Stored in Windows Credential Manager. Leave blank to keep the saved key."><input type="password" autoComplete="off" value={key} onChange={e => setKey(e.target.value)} placeholder="Paste your key here, never in chat" /></Field>
      <Toggle label="Allow assigned context to leave this computer" description="Helpers receive their assigned prompt and tool results. Goal review sends the goal, checklist and recorded evidence to OpenRouter and its model provider." checked={Boolean(provider.remote_consent)} disabled={busy} onChange={remote_consent => setProvider({ ...provider, remote_consent })} />
      <Toggle label="Enable free OpenRouter agents" checked={Boolean(provider.enabled)} disabled={busy} onChange={enabled => setProvider({ ...provider, enabled })} />
      <Toggle label="Allow providers that collect data" description="Off requests providers that do not collect prompt data. Free providers may be unavailable under this restriction." checked={provider.data_collection === 'allow'} disabled={busy} onChange={allow => setProvider({ ...provider, data_collection: allow ? 'allow' : 'deny' })} />
      <div className="setup-actions"><button className="primary" disabled={busy} onClick={() => void save()}><KeyRound size={14} />Save connection</button><button className="secondary" disabled={busy} onClick={() => void save(true)}><Check size={14} />Connect / Test</button><a href="https://openrouter.ai/settings/keys" target="_blank" rel="noopener noreferrer">Create a key</a></div>
      {quota && <p className="openrouter-quota">Free requests today: {quota.used} / {quota.limit} · {quota.remaining} remaining. Helpers and reviews share this quota.</p>}
    </section>
    {settings && onChange && <>
      <section className="settings-card"><h3>Goal guidance and workflow verification</h3><Toggle label="Use OpenRouter guidance for goals" checked={Boolean(settings.goal_cloud_guidance ?? workflow?.guidance_enabled)} disabled={busy} description="A scoped planner advises ordinary goals and Builder tasks before local implementation. Advisory outages continue locally after 60 seconds." onChange={value=>void change({goal_cloud_guidance:value})}/><div className="workflow-connection-states">{[['Configuration',workflow?.configuration],['Account authentication',workflow?.authentication],['Free capacity',workflow?.free_capacity],['Free catalogue',workflow?.catalog],['Profiles',workflow?.profiles],['Goal workflow',workflow?.workflow]].map(([name,value])=>{const state=value as {state?:string;checked_at?:string}|undefined;return <div key={String(name)}><strong>{String(name)}</strong><Badge>{state?.state || 'unverified'}</Badge><small>{state?.checked_at ? new Date(state.checked_at).toLocaleString() : 'No verification recorded'}</small></div>;})}</div>{workflow?.profiles?.missing?.length ? <p>Missing profiles: {workflow.profiles.missing.join(', ')}</p> : null}<div className="setup-actions"><button className="primary" disabled={busy||!ready} onClick={()=>void setupWorkflow()}>Set up goal connections</button><button className="secondary" disabled={busy||!ready||selfTest?.state==='running'} onClick={()=>void testWorkflow()}>Verify goal workflow</button><button className="text-button" disabled={busy} onClick={()=>void refreshWorkflow().catch(e=>setError(errorText(e)))}>Refresh status</button></div>{selfTest && <details open><summary>Workflow check · {selfTest.state}</summary><small>Journal {selfTest.id}</small>{selfTest.failure_reason && <p role="alert">{selfTest.failure_reason}</p>}<pre>{JSON.stringify(selfTest.stages || [],null,2)}</pre></details>}<p className="muted">Account authentication and profile configuration do not establish a completed goal workflow. The live check records planner, local execution, checks and independent review.</p></section>
      <section className="openrouter-setup"><Sparkles size={18} /><div><strong>Local lead, remote team</strong><small>Set up a Researcher, a read-only Assistant and a separate goal reviewer. The main agent can request help as it works.</small></div><button className="primary" disabled={busy || !ready} onClick={() => void setupAgents()}>Set up helpers &amp; reviewer</button></section>
      <section className="openrouter-setup"><Bot size={18}/><div><strong>Builder specialists</strong><small>Free planning, design, coding advice, diagnosis, research and review. The local agent applies all changes.</small></div><button className="secondary" disabled={busy || !ready} onClick={()=>void setupSpecialists()}>Set up Builder specialists</button></section>
      <Toggle label="Require cloud review for Builder completion" description="Off allows local completion after verified gates when the free reviewer is unavailable. Valid review findings still require repairs." checked={Boolean(settings.cloud_required_review)} onChange={value=>void change({cloud_required_review:value})}/>
      {!ready && <p className="muted openrouter-setup-hint">Save an enabled connection with context consent, then Connect / Test to finish setup.</p>}
      <section className="settings-card">
        <div className="openrouter-section-title"><Bot size={17} /><div><h3>Helpers</h3><small>Focused assistance requested by the main agent</small></div></div>
        <Toggle label="Automatic delegation" description="Expose enabled profiles to the main agent so it can request research or read-only help. Profiles retain their own model, tools and limits." checked={Boolean(settings.auto_delegate)} disabled={busy || !ready && !settings.auto_delegate} onChange={auto_delegate => void change({ auto_delegate })} />
        <p className="muted">Edit helper instructions, models and tool scopes in Agents.</p>
      </section>
      <section className="settings-card">
        <div className="openrouter-section-title"><ShieldCheck size={17} /><div><h3>Independent goal reviewer</h3><small>Separate OpenRouter run · Read only</small></div></div>
        <Toggle label="Verify goals before completion" description="When the main agent claims it is done, review its result against the goal and recorded evidence. Requested fixes return to the main agent for another pass." checked={Boolean(settings.goal_review_enabled)} disabled={busy || !ready && !settings.goal_review_enabled} onChange={goal_review_enabled => void change({ goal_review_enabled })} />
        <div className="form-grid"><Field label="Reviewer model"><select value={reviewModel} disabled={busy} onChange={e => void change({ goal_review_model: e.target.value })}>{models.map(model => <option value={model} key={model}>{model === FREE_ROUTER ? 'Free models router' : model}</option>)}</select></Field><Field label="Correction passes" hint="Pause for your review if this limit is reached."><select value={Number(settings.goal_review_max_revisions || 3)} disabled={busy} onChange={e => void change({ goal_review_max_revisions: Number(e.target.value) })}>{[1,2,3,4,5].map(count => <option key={count} value={count}>{count}</option>)}</select></Field><Field label="Review context" hint="A bounded evidence packet; does not change the main model’s context."><select value={reviewContext} disabled={busy} onChange={e => void change({ goal_review_context: Number(e.target.value) })}>{reviewContexts.map(context => <option key={context} value={context}>{tokenLabel(context)}</option>)}</select></Field></div>
        <p className="muted">Verdicts and feedback appear in Goals and Activity. Missing evidence, quota limits or connection errors leave the goal paused without verified completion.</p>
      </section>
    </>}
    <p className="muted openrouter-footnote">Forge uses advertised free models and zero-price routes, with no paid fallback. Availability and request limits still apply. Helpers and review keep the same permission boundaries as the main agent.</p>
  </div>;
}
