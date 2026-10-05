import { useEffect, useState } from 'react';
import { api } from './api';
import { Field } from './components';
import type { Agent } from './types';
export function AgentProviderFields({ agent, models, update }: { agent: Agent; models: { name: string }[]; update: (v: Agent) => void }) {
  const [providers, setProviders] = useState<{ id: string; name: string }[]>([]); const [choices, setChoices] = useState(models);
  useEffect(() => { void api<{ providers: { id: string; name: string }[] }>('providers').then(r => setProviders(r.providers)); }, []);
  useEffect(() => { let disposed = false; if (!agent.provider_id) { setChoices(models); return; } void api<{ models: { name: string }[] }>('models', { provider_id: agent.provider_id }).then(r => { if (!disposed) setChoices(r.models); }).catch(() => { if (!disposed) setChoices(agent.provider_id === 'openrouter' ? [{ name: 'openrouter/free' }] : []); }); return () => { disposed = true; }; }, [agent.provider_id, models]);
  return <><Field label="Agent engine"><select value={String(agent.provider_id || '')} onChange={e => update({ ...agent, provider_id: e.target.value, model: e.target.value === 'openrouter' ? 'openrouter/free' : '' })}><option value="">Inherit parent engine</option>{providers.map(p => <option value={p.id} key={p.id}>{p.name}</option>)}</select></Field><Field label="Model"><select value={agent.model || ''} onChange={e => update({ ...agent, model: e.target.value })}><option value="">Inherit from parent</option>{choices.map(m => <option key={m.name}>{m.name}</option>)}</select></Field>{agent.provider_id === 'openrouter' && <p className="muted">Remote free-only inference · Assigned context is sent to OpenRouter. Free quotas apply.</p>}</>;
}
