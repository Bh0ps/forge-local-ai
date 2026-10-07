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
const FREE_ROUTER = 'openrouter/free';
const connectionOptions = (provider: Provider) => ({ enabled: Boolean(provider.enabled), remote_consent: Boolean(provider.remote_consent), data_collection: provider.data_collection || 'deny' });

export function OpenRouterSettings({ notify, settings, onChange }: { notify: (v: string) => void; settings?: Settings; onChange?: (v: Partial<Settings>) => Promise<void> }) {
  const [provider, setProvider] = useState<Provider>({});
  const [savedProvider, setSavedProvider] = useState<Provider>({});
  const [key, setKey] = useState(''); const [error, setError] = useState(''); const [busy, setBusy] = useState(false);
  useEffect(() => {
    let disposed = false;
    void api<{ provider: Provider }>('openrouter_status').then(result => { if (!disposed) { setProvider(result.provider || {}); setSavedProvider(result.provider || {}); } }).catch(e => { if (!disposed) setError(errorText(e)); });
    return () => { disposed = true; };
  }, []);
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
  return <div className="openrouter-settings">
    <div className="openrouter-title"><div><h2>OpenRouter team</h2><p className="muted">Give your local agent remote help and an independent completion check.</p></div><Badge>{savedProvider.connected ? 'Connected' : 'Setup needed'}</Badge></div>
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
      <section className="openrouter-setup"><Sparkles size={18} /><div><strong>Local lead, remote team</strong><small>Set up a Researcher, a read-only Assistant and a separate goal reviewer. The main agent can request help as it works.</small></div><button className="primary" disabled={busy || !ready} onClick={() => void setupAgents()}>Set up helpers &amp; reviewer</button></section>
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
