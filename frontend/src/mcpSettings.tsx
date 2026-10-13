import { useEffect, useRef, useState } from 'react';
import { Check, ChevronRight, ExternalLink, Eye, EyeOff, LoaderCircle, Plug, Plus, Power, Settings2, ShieldCheck, Wrench } from 'lucide-react';
import { api, errorText } from './api';
import { Badge, Empty, ErrorNotice, Field, IconButton, Modal, Toggle, useResource } from './components';
import type { Connection } from './types';
import './mcpSettings.css';

export interface McpServer extends Connection { args?: string[]; cwd?: string; authenticated?: boolean; auth_type?: 'none' | 'bearer' | 'oauth'; enabled_tools?: string[]; tools?: { name: string; description?: string }[]; }
type AuthState = { state: string; setup_token?: string; flow_id?: string; authorization_url?: string; server?: McpServer };
type CheckResult = { tool_count: number; tools?: { name: string; description?: string }[] };
const identity = () => crypto.randomUUID().replaceAll('-', '');

export function McpSettings({ notify }: { notify: (message: string) => void }) {
  const data = useResource<McpServer>('mcp_servers', 'servers');
  const [edit, setEdit] = useState<McpServer | null>(null), [editor, setEditor] = useState(0);
  const [tools, setTools] = useState<McpServer | null>(null), [pending, setPending] = useState('');
  const changing = useRef(false);
  function open(server: McpServer) { setEditor(current => current + 1); setEdit(server); }
  async function toggle(server: McpServer) {
    if (changing.current) return;
    changing.current = true; setPending(server.id);
    try { await api('mcp_save', { id: server.id, enabled: !server.enabled }); await data.refresh(); }
    catch (error) { notify(errorText(error)); }
    finally { changing.current = false; setPending(''); }
  }
  return <><div className="settings-section-heading"><h2>MCP connections</h2><button className="secondary" onClick={() => open({ id: '', name: '', url: '', transport: 'http', auth_type: 'none', enabled: false })}><Plus size={14} />Add connection</button></div>
    <p className="muted">Connect the tools you use. Check a connection before saving it.</p>
    {data.error && <ErrorNotice error={data.error} retry={() => void data.refresh()} />}
    {data.items.length ? <section className="settings-card mcp-connections">{data.items.map(server => <div className="mcp-connection-row" key={server.id}>
      <span className={`mcp-connection-icon${server.enabled ? ' enabled' : ''}`}><Plug size={19} aria-hidden="true" /></span>
      <div className="mcp-connection-name"><strong>{server.name}</strong><small title={server.url || server.command}>{server.url || server.command}</small></div>
      <Badge>{server.enabled ? 'Enabled' : ['needs_test', 'authenticating', 'error'].includes(server.status || '') ? 'Needs setup' : 'Disabled'}</Badge>
      <div className="mcp-row-actions"><IconButton label={`Configure ${server.name}`} onClick={() => open(server)}><Settings2 size={16} /></IconButton><IconButton label={`Tools for ${server.name}`} onClick={() => setTools(server)}><Wrench size={16} /></IconButton><IconButton label={`${server.enabled ? 'Disable' : 'Enable'} ${server.name}`} disabled={Boolean(pending)} onClick={() => void toggle(server)}>{pending === server.id ? <LoaderCircle size={16} className="spin" /> : <Power size={16} />}</IconButton></div>
    </div>)}</section> : !data.error && <Empty icon={<Plug size={26} />} title="Connect your first tool server" text="Start with an endpoint URL. Local executable servers are available in advanced setup." />}
    {edit && <McpEditor key={editor} server={edit} close={() => { setEdit(null); void data.refresh(); }} committed={() => void data.refresh()} complete={() => { setEdit(null); void data.refresh(); notify('MCP connection enabled.'); }} />}
    {tools && <Modal title={`${tools.name} · Tools`} close={() => setTools(null)} wide><McpTools connection={tools} notify={notify} /></Modal>}
  </>;
}

export function McpEditor({ server, close, committed, complete }: { server: McpServer; close: () => void; committed: () => void; complete: () => void }) {
  const [draft, setDraft] = useState({ ...server }), [args, setArgs] = useState(JSON.stringify(server.args || []));
  const [token, setToken] = useState(''), [showToken, setShowToken] = useState(false), [advanced, setAdvanced] = useState(server.transport === 'stdio');
  const [result, setResult] = useState<CheckResult | null>(null), [error, setError] = useState(''), [phase, setPhase] = useState('');
  const [auth, setAuth] = useState<AuthState | null>(null), [saved, setSaved] = useState(false);
  const generation = useRef(0), pending = useRef(false), entityId = useRef(server.id || identity()), savedId = useRef(server.id);
  const nameInput = useRef<HTMLInputElement>(null);
  const probe = useRef<string | null>(null), flow = useRef<{ id: string; flow_id: string } | null>(null);
  function discard() {
    generation.current++; pending.current = false;
    if (probe.current) { void api('mcp_probe_cancel', { probe_id: probe.current }).catch(() => {}); probe.current = null; }
    if (flow.current) { void api('mcp_auth_cancel', flow.current).catch(() => {}); flow.current = null; }
  }
  useEffect(() => { nameInput.current?.focus(); return () => { discard(); }; }, []);
  function cancel() { discard(); setToken(''); setShowToken(false); close(); }
  function change(delta: Partial<McpServer>, nextArgs?: string) {
    discard(); setDraft(current => ({ ...current, ...delta }));
    if (nextArgs !== undefined) setArgs(nextArgs);
    setToken(''); setShowToken(false); setResult(null); setAuth(null); setError(''); setPhase('');
  }
  function payload() {
    let parsed: unknown;
    try { parsed = args.trim() ? JSON.parse(args) : []; } catch { throw new Error('Use a JSON array for arguments, for example ["server.py"].'); }
    if (!Array.isArray(parsed) || !parsed.every(item => typeof item === 'string')) throw new Error('Arguments must be a JSON array of strings.');
    return { name: draft.name.trim(), transport: draft.transport || 'http', url: draft.url?.trim(), command: draft.command?.trim(), args: parsed, cwd: draft.cwd, auth_type: draft.auth_type };
  }
  async function check() {
    if (pending.current) return;
    pending.current = true; const request = generation.current, probeId = identity();
    probe.current = probeId; setPhase('Checking connection…'); setError(''); setResult(null);
    try {
      const checked = await api<CheckResult>('mcp_probe', { probe_id: probeId, id: savedId.current || undefined, draft: payload(), token: token || undefined });
      if (request !== generation.current) return;
      if (!Number.isSafeInteger(checked.tool_count) || checked.tool_count < 0) throw new Error('The server did not return a usable tool catalog.');
      setResult(checked);
    } catch (failure) { if (request === generation.current) setError(errorText(failure)); }
    finally { if (request === generation.current) { probe.current = null; pending.current = false; setPhase(''); } }
  }
  async function save(oauth = false) {
    if (pending.current || (auth && auth.state !== 'ready') || (!oauth && !result)) return;
    pending.current = true; const request = generation.current;
    setPhase('Saving connection…'); setError('');
    try {
      const values = payload();
      if (!values.name) throw new Error('Give this connection a name before saving.');
      const response = await api<{ server: McpServer }>('mcp_save', { ...values, id: entityId.current, token: token || undefined, enabled: false });
      committed();
      if (request !== generation.current) return;
      savedId.current = response.server.id; setSaved(true); setToken(''); setShowToken(false);
      if (oauth) {
        const attempt = { id: response.server.id, flow_id: identity() }; flow.current = attempt;
        setPhase('Preparing sign-in…');
        const started = await api<AuthState>('mcp_auth_start', attempt);
        if (request !== generation.current) { void api('mcp_auth_cancel', attempt).catch(() => {}); return; }
        if (!['connecting', 'authenticate'].includes(started.state)) throw new Error('The server did not start a usable sign-in session. Try again.');
        setAuth({ ...started, flow_id: attempt.flow_id }); setResult(null);
      } else {
        setPhase('Verifying saved connection…');
        const checked = await api<{ tools: McpServer['tools']; setup_token: string }>('mcp_test', { id: response.server.id });
        if (request !== generation.current) return;
        if (!Array.isArray(checked.tools) || typeof checked.setup_token !== 'string' || !checked.setup_token) throw new Error('The saved connection did not return a usable tool catalog.');
        await api('mcp_save', { id: response.server.id, enabled: true, setup_token: checked.setup_token }); committed();
        if (request === generation.current) complete();
      }
    } catch (failure) { if (request === generation.current) {
      if (oauth && flow.current) { void api('mcp_auth_cancel', flow.current).catch(() => {}); flow.current = null; }
      setResult(null); setError(errorText(failure));
    } }
    finally { if (request === generation.current) { pending.current = false; setPhase(''); } }
  }
  useEffect(() => {
    if (!auth || auth.state === 'ready' || !flow.current) return;
    const active = { ...flow.current }, request = generation.current;
    let disposed = false, timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const next = await api<AuthState>('mcp_auth_status', active);
        if (disposed || request !== generation.current) return;
        if (!['ready', 'connecting', 'authenticate'].includes(next.state)) throw new Error('The server did not return a usable sign-in status. Try again.');
        if (next.state === 'ready' && (!Array.isArray(next.server?.tools) || typeof next.setup_token !== 'string' || !next.setup_token)) throw new Error('Sign-in did not return a usable tool catalog. Sign in again.');
        if (next.state === 'ready') { setAuth(next); setResult({ tool_count: next.server?.tools?.length || 0, tools: next.server?.tools }); committed(); }
        else { setAuth(next); timer = setTimeout(() => void poll(), 1000); }
      } catch (failure) {
        if (!disposed && request === generation.current) { setError(errorText(failure)); setAuth(null); void api('mcp_auth_cancel', active).catch(() => {}); flow.current = null; }
      }
    }
    timer = setTimeout(() => void poll(), 500);
    return () => { disposed = true; clearTimeout(timer); };
  }, [auth?.state, auth?.flow_id]);
  async function enable() {
    if (pending.current || !savedId.current || !auth?.setup_token) return;
    pending.current = true; const request = generation.current; setPhase('Enabling connection…'); setError('');
    try { await api('mcp_save', { id: savedId.current, enabled: true, setup_token: auth.setup_token }); committed(); flow.current = null; if (request === generation.current) complete(); }
    catch (failure) { if (request === generation.current) {
      setAuth(null); setResult(null);
      if (flow.current) { void api('mcp_auth_cancel', flow.current).catch(() => {}); flow.current = null; }
      setError(errorText(failure));
    } }
    finally { if (request === generation.current) { pending.current = false; setPhase(''); } }
  }
  const authenticating = Boolean(auth && auth.state !== 'ready'), busy = Boolean(phase), locked = busy || authenticating;
  const oauth = draft.auth_type === 'oauth', legacyAuth = !server.auth_type && server.authenticated && !draft.auth_type;
  const reuseToken = server.authenticated && draft.url === server.url && draft.transport === server.transport && draft.auth_type === server.auth_type;
  return <Modal title={server.id ? 'Configure MCP connection' : 'Add MCP connection'} close={cancel}><form className="mcp-editor" aria-busy={busy} onSubmit={event => { event.preventDefault(); if (auth?.state === 'ready') void enable(); else void save(oauth); }}>
    <p className="mcp-editor-intro">{oauth ? 'Sign-in requires saving this connection. Its tools stay disabled until you enable it.' : 'Test your connection before saving. Testing keeps your current setup in place.'}</p>
    <Field label="Name"><input ref={nameInput} autoFocus required value={draft.name} disabled={locked} onChange={event => { setDraft(current => ({ ...current, name: event.target.value })); setError(''); }} placeholder="My research tools" /></Field>
    {advanced ? <><Field label="Executable"><input required value={draft.command || ''} disabled={locked} onChange={event => change({ command: event.target.value, transport: 'stdio' })} placeholder="Executable path" /></Field><Field label="Arguments (JSON array)" hint="Testing starts this local server."><input value={args} disabled={locked} onChange={event => change({}, event.target.value)} placeholder='["server.py"]' /></Field></> : <><Field label="Endpoint URL"><input required type="url" value={draft.url || ''} disabled={locked} onChange={event => change({ url: event.target.value })} placeholder="https://example.com/mcp" /></Field>
      <div className="mcp-editor-grid"><Field label="Transport"><select value={draft.transport || 'http'} disabled={locked} onChange={event => change({ transport: event.target.value })}><option value="http">Streamable HTTP</option><option value="sse">Legacy SSE</option></select></Field><Field label="Authentication"><select value={draft.auth_type || (legacyAuth ? 'saved' : 'none')} disabled={locked} onChange={event => change({ auth_type: event.target.value === 'saved' ? undefined : event.target.value as McpServer['auth_type'] })}>{!server.auth_type && server.authenticated && <option value="saved">Saved credentials</option>}<option value="none">None</option><option value="bearer">Access token</option><option value="oauth">Browser sign-in</option></select></Field></div>
      {legacyAuth && <p className="mcp-saved-note">Saved credentials apply only to the original endpoint and transport.</p>}
      {draft.auth_type === 'bearer' && <Field label="Access token" hint={reuseToken ? 'Leave blank to use the saved token for this exact endpoint.' : 'Stored in the operating system credential store when you save.'}><div className="mcp-password"><input aria-label="Access token" type={showToken ? 'text' : 'password'} autoComplete="off" value={token} disabled={locked} onChange={event => { setToken(event.target.value); setResult(null); setError(''); }} /><IconButton label={showToken ? 'Hide access token' : 'Show access token'} disabled={locked} onClick={() => setShowToken(!showToken)}>{showToken ? <EyeOff size={16} /> : <Eye size={16} />}</IconButton></div></Field>}</>}
    <button type="button" className="text-button mcp-advanced-link" disabled={locked} onClick={() => { setAdvanced(!advanced); change({ transport: advanced ? 'http' : 'stdio', auth_type: 'none' }); }}><ChevronRight size={13} />{advanced ? 'Use a remote URL' : 'Advanced: local executable'}</button>
    {busy && <div className="mcp-setup-state" role="status"><LoaderCircle size={16} className="spin" aria-hidden="true" />{phase}</div>}
    {authenticating && <div className="mcp-setup-state"><ShieldCheck size={17} aria-hidden="true" /><div><strong>{auth?.authorization_url ? 'Continue sign-in in your browser' : 'Waiting for sign-in'}</strong>{auth?.authorization_url && <a href={auth.authorization_url} target="_blank" rel="noopener noreferrer">Open sign-in<ExternalLink size={12} /></a>}</div></div>}
    {result && !busy && <div className="mcp-setup-state success" role="status"><Check size={16} aria-hidden="true" /><div><strong>{auth?.state === 'ready' ? 'Signed in' : 'Connection verified'}</strong><p>{result.tool_count} {result.tool_count === 1 ? 'tool is' : 'tools are'} available.{!result.tool_count && ' This server does not advertise tools yet.'}</p>{result.tools?.length ? <div className="mcp-tool-preview">{result.tools.slice(0, 3).map(tool => <span key={tool.name}>{tool.name}</span>)}</div> : null}</div></div>}
    {error && <ErrorNotice error={error} />}{saved && !authenticating && error && <p className="mcp-saved-note">The connection was saved. Its tools stay disabled until setup finishes.</p>}
    <footer><button type="button" className="text-button mcp-cancel" onClick={cancel}>{saved ? 'Close' : 'Cancel'}</button>{authenticating ? <button type="button" className="secondary" onClick={() => { discard(); setAuth(null); setResult(null); setPhase(''); }}>Cancel sign-in</button> : auth?.state === 'ready' ? <button type="button" className="primary" disabled={busy} onClick={() => void enable()}><Power size={14} />Enable connection</button> : <>{!oauth && <button type="button" className="secondary" disabled={busy} onClick={() => void check()}><Plug size={14} />Test connection</button>}<button className="primary" disabled={busy || (!oauth && !result)}>{oauth ? <ShieldCheck size={14} /> : <Check size={14} />}{oauth ? 'Save & sign in' : 'Save & enable'}</button></>}</footer>
  </form></Modal>;
}

function McpTools({ connection, notify }: { connection: McpServer; notify: (message: string) => void }) {
  const all = connection.tools || [], [enabled, setEnabled] = useState(connection.enabled_tools || all.map(tool => tool.name)), [busy, setBusy] = useState(false), pending = useRef(false);
  async function toggle(name: string, value: boolean) {
    if (pending.current) return;
    pending.current = true; setBusy(true); const next = value ? [...enabled, name] : enabled.filter(item => item !== name);
    try { await api('mcp_save', { id: connection.id, enabled_tools: next }); setEnabled(next); }
    catch (failure) { notify(errorText(failure)); }
    finally { pending.current = false; setBusy(false); }
  }
  return <div className="mcp-tools">{all.length ? all.map(tool => <Toggle key={tool.name} label={tool.name} description={tool.description} checked={enabled.includes(tool.name)} disabled={busy} onChange={value => void toggle(tool.name, value)} />) : <Empty icon={<Wrench size={22} />} title="No tools discovered" text="Check this connection to discover its available tools." />}</div>;
}
