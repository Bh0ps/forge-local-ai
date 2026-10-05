import { useEffect, useState } from 'react';
import { Check, ChevronRight, ExternalLink, Plug, Plus } from 'lucide-react';
import { api, errorText } from './api';
import { Badge, Empty, ErrorNotice, Field, Modal, Toggle, useResource } from './components';
import type { Connection } from './types';
interface Server extends Connection { args?: string[]; auth_type?: 'none' | 'bearer' | 'oauth'; enabled_tools?: string[]; tools?: { name: string; description?: string }[]; }
type AuthState = { state: string; authorization_url?: string; server?: Server };
export function McpSettings({ notify }: { notify: (message: string) => void }) {
  const data = useResource<Server>('mcp_servers', 'servers');
  const [edit, setEdit] = useState<Server | null>(null); const [advanced, setAdvanced] = useState(false); const [args, setArgs] = useState(''); const [token, setToken] = useState(''); const [result, setResult] = useState<{ error?: string; tools?: unknown[]; server?: Server } | null>(null); const [busy, setBusy] = useState(false); const [tools, setTools] = useState<Server | null>(null); const [auth, setAuth] = useState<AuthState | null>(null);
  function close() { if (auth && edit?.id) void api('mcp_auth_cancel', { id: edit.id }).catch(() => {}); setEdit(null); setToken(''); setAuth(null); void data.refresh(); }
  useEffect(() => {
    if (!auth || !edit?.id || auth.state === 'ready') return;
    const id = edit.id; let fetching = false;
    const timer = setInterval(async () => {
      if (fetching) return; fetching = true;
      try { const next = await api<AuthState>('mcp_auth_status', { id }); setAuth(next); if (next.state === 'ready') { setResult({ tools: next.server?.tools || [], server: next.server }); if (next.server) setEdit(next.server); } }
      catch (e) { setResult({ error: errorText(e) }); setAuth(null); }
      finally { fetching = false; }
    }, 900); return () => clearInterval(timer);
  }, [auth?.state, edit?.id]);
  async function draft() {
    if (!edit) throw new Error('Add a connection first.');
    const parsed = args.trim() ? JSON.parse(args) : [];
    if (!Array.isArray(parsed) || !parsed.every(value => typeof value === 'string')) throw new Error('Arguments must be a JSON array of strings.');
    const response = await api<{ server: Server }>('mcp_save', { ...edit, id: edit.id || undefined, args: parsed, token: token || undefined, enabled: false }); setEdit(response.server); setToken(''); return response.server;
  }
  async function test() {
    setBusy(true); setResult(null);
    try { const server = await draft(); if (server.auth_type === 'oauth') { const flow = await api<AuthState>('mcp_auth_start', { id: server.id }); setAuth(flow); } else setResult(await api('mcp_test', { id: server.id })); }
    catch (e) { setResult({ error: errorText(e) }); }
    finally { setBusy(false); }
  }
  async function enable() { if (!edit) return; setBusy(true); try { await api('mcp_save', { id: edit.id, enabled: true }); setAuth(null); setEdit(null); await data.refresh(); notify('MCP connection enabled.'); } catch (e) { notify(errorText(e)); } finally { setBusy(false); } }
  return <><div className="settings-section-heading"><h2>MCP connections</h2><button className="secondary" onClick={() => { setEdit({ id: '', name: '', url: '', transport: 'http', auth_type: 'none', enabled: false }); setAdvanced(false); setResult(null); setArgs(''); setToken(''); setAuth(null); }}><Plus size={14} />Add connection</button></div><p className="muted">Connect a server, authenticate if required, then enable its tools.</p>{data.error && <ErrorNotice error={data.error} retry={() => void data.refresh()} />}{data.items.length ? <section className="settings-card">{data.items.map(connection => <div className="connection-row" key={connection.id}><Plug size={21} /><div><strong>{connection.name}</strong><small>{connection.url || connection.command}</small></div><Badge>{connection.enabled ? connection.status || 'Enabled' : 'Disabled'}</Badge><button className="text-button" onClick={() => setTools(connection)}>Tools</button><button className="text-button" onClick={() => { setEdit(connection); setAdvanced(connection.transport === 'stdio'); setArgs(JSON.stringify(connection.args || [])); setResult(null); setToken(''); setAuth(null); }}>Edit</button><button className="text-button" onClick={() => void api('mcp_save', { id: connection.id, enabled: !connection.enabled }).then(() => void data.refresh()).catch(e => notify(errorText(e)))}>{connection.enabled ? 'Disable' : 'Enable'}</button></div>)}</section> : !data.error && <Empty icon={<Plug size={26} />} title="Connect your first tool server" text="Paste an MCP endpoint URL. Local executable servers are available in advanced setup." />}
    {edit && <Modal title="MCP connection" close={close}><Field label="Name"><input autoFocus value={edit.name} onChange={e => { setEdit({ ...edit, name: e.target.value }); setResult(null); }} placeholder="My tools" /></Field>{advanced ? <><Field label="Executable"><input value={edit.command || ''} onChange={e => { setEdit({ ...edit, command: e.target.value, transport: 'stdio' }); setResult(null); }} placeholder="Executable path" /></Field><Field label="Arguments (JSON array)"><input value={args} onChange={e => { setArgs(e.target.value); setResult(null); }} placeholder='["server.py"]' /></Field></> : <><Field label="Endpoint URL"><input value={edit.url || ''} onChange={e => { setEdit({ ...edit, url: e.target.value }); setResult(null); }} placeholder="https://example.com/mcp" /></Field><Field label="Transport"><select value={edit.transport || 'http'} onChange={e => { setEdit({ ...edit, transport: e.target.value }); setResult(null); }}><option value="http">Streamable HTTP</option><option value="sse">Legacy SSE</option></select></Field><Field label="Authentication"><select value={edit.auth_type || 'none'} onChange={e => { setEdit({ ...edit, auth_type: e.target.value as Server['auth_type'] }); setResult(null); }}><option value="none">None</option><option value="bearer">Access token</option><option value="oauth">Sign in with browser (OAuth)</option></select></Field>{edit.auth_type === 'bearer' && <Field label="Access token"><input type="password" autoComplete="off" value={token} onChange={e => { setToken(e.target.value); setResult(null); }} /></Field>}</>}<button className="text-button" onClick={() => { setAdvanced(!advanced); setEdit({ ...edit, transport: !advanced ? 'stdio' : 'http' }); setResult(null); }}><ChevronRight size={13} />{advanced ? 'Use a remote URL' : 'Advanced: local executable'}</button>{auth && auth.state !== 'ready' && <div className="mcp-test"><span>{auth.authorization_url ? 'Continue authentication in your browser.' : 'Preparing authentication…'}</span>{auth.authorization_url && <a href={auth.authorization_url} target="_blank" rel="noopener noreferrer">Sign in<ExternalLink size={12} /></a>}</div>}{result && <div className={`mcp-test ${result.error ? 'error' : ''}`} role="status">{result.error ? String(result.error) : <><Check size={15} /><span>Connected. {result.tools?.length || 0} tools available.</span></>}</div>}<footer><button className="secondary" onClick={() => void test()} disabled={busy || !edit.name || Boolean(auth && auth.state !== 'ready')}>{busy ? 'Connecting…' : 'Connect / Test'}</button><button className="primary" onClick={() => void enable()} disabled={busy || !result || Boolean(result.error)}>Enable connection</button></footer></Modal>}
    {tools && <Modal title={`${tools.name} · Tools`} close={() => setTools(null)} wide><McpTools connection={tools} notify={notify} /></Modal>}
  </>;
}
function McpTools({ connection, notify }: { connection: Server; notify: (message: string) => void }) {
  const all = connection.tools || []; const [enabled, setEnabled] = useState(connection.enabled_tools || all.map(tool => tool.name)); const [busy, setBusy] = useState(false);
  async function toggle(name: string, value: boolean) { const next = value ? [...enabled, name] : enabled.filter(item => item !== name); setBusy(true); try { await api('mcp_save', { id: connection.id, enabled_tools: next }); setEnabled(next); } catch (e) { notify(errorText(e)); } finally { setBusy(false); } }
  return <div className="mcp-tools">{all.length ? all.map(tool => <Toggle key={tool.name} label={tool.name} description={tool.description} checked={enabled.includes(tool.name)} disabled={busy} onChange={value => void toggle(tool.name, value)} />) : <Empty icon={<Plug size={22} />} title="No tools discovered" text="Test this connection to discover its available tools." />}</div>;
}
