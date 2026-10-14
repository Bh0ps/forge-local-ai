import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest';

const key = 'forge.browser-client-proof.v1';
const proof = 'synthetic-proof-'.repeat(5);
const nextProof = 'next-synthetic-proof-'.repeat(5);
function response(body: unknown, ok = true, status = 200) { return {ok, status, json: async () => body}; }
beforeEach(() => { vi.resetModules(); window.localStorage.clear(); delete window.pywebview; });
afterEach(() => { vi.unstubAllGlobals(); });

describe('Browser pairing proof isolation', () => {
  it('saves only a successful pairing proof and adds it to same-origin API calls after reload', async () => {
    const fetch = vi.fn().mockResolvedValueOnce(response({ok:true, client_proof:proof})).mockResolvedValue(response({ok:true}));
    vi.stubGlobal('fetch', fetch);
    let {api} = await import('./api');
    await api('pair', {code:'synthetic'});
    expect(JSON.parse(window.localStorage.getItem(key)!)).toEqual({origin:window.location.origin, value:proof});
    vi.resetModules(); ({api} = await import('./api'));
    await api('poll', {id:'run', after:2});
    expect(fetch).toHaveBeenLastCalledWith('/api/v1/poll', expect.objectContaining({
      headers:{'Content-Type':'application/json','X-Forge-Client-Proof':proof}, credentials:'same-origin', redirect:'error',
      body:JSON.stringify({id:'run',after:2}),
    }));
    expect(fetch.mock.calls[0][1].headers).not.toHaveProperty('X-Forge-Client-Proof');
  });

  it('does not reuse a different origin or port proof and preserves credentials after a rejected code', async () => {
    window.localStorage.setItem(key, JSON.stringify({origin:'http://127.0.0.1:9999',value:proof}));
    const fetch = vi.fn().mockResolvedValueOnce(response({error:'Invalid pairing code'}, false,403)).mockResolvedValue(response({ok:true}));
    vi.stubGlobal('fetch', fetch);
    const {api} = await import('./api');
    await expect(api('pair', {code:'bad'})).rejects.toThrow('Invalid pairing code');
    await api('bootstrap');
    expect(fetch.mock.calls[1][1].headers).not.toHaveProperty('X-Forge-Client-Proof');
    expect(JSON.parse(window.localStorage.getItem(key)!)).toEqual({origin:'http://127.0.0.1:9999',value:proof});
  });

  it('keeps the new proof when an older unpair request resolves late', async () => {
    const fetch = vi.fn().mockResolvedValueOnce(response({ok:true,client_proof:proof}));
    vi.stubGlobal('fetch', fetch);
    const {api} = await import('./api');
    await api('pair', {code:'first'});
    let finish!: (value: unknown) => void;
    fetch.mockImplementationOnce(() => new Promise(resolve => {finish=resolve;}));
    const oldUnpair = api('unpair');
    fetch.mockResolvedValueOnce(response({ok:true,client_proof:nextProof}));
    await api('pair', {code:'second'});
    finish(response({ok:true})); await oldUnpair;
    expect(JSON.parse(window.localStorage.getItem(key)!).value).toBe(nextProof);
    fetch.mockResolvedValueOnce(response({ok:true})); await api('poll', {id:'same',after:2});
    expect(fetch.mock.calls.at(-1)![1].headers['X-Forge-Client-Proof']).toBe(nextProof);
  });

  it('clears successful unpair and handles restricted storage within the current tab', async () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {throw new Error('Unavailable');});
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {throw new Error('Unavailable');});
    vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(() => {throw new Error('Unavailable');});
    const fetch = vi.fn().mockResolvedValueOnce(response({ok:true,client_proof:proof})).mockResolvedValue(response({ok:true}));
    vi.stubGlobal('fetch', fetch);
    const {api} = await import('./api');
    await api('pair', {code:'synthetic'}); await api('poll');
    expect(fetch.mock.calls.at(-1)![1].headers['X-Forge-Client-Proof']).toBe(proof);
    await api('unpair'); await api('bootstrap');
    expect(fetch.mock.calls.at(-1)![1].headers).not.toHaveProperty('X-Forge-Client-Proof');
  });

  it('leaves native bridge calls outside browser authentication', async () => {
    const fetch = vi.fn(); vi.stubGlobal('fetch',fetch);
    const call = vi.fn().mockResolvedValue({ok:true}); window.pywebview={api:{call}};
    const {api}=await import('./api'); await api('poll',{id:'native'});
    expect(call).toHaveBeenCalledWith('poll',{id:'native'}); expect(fetch).not.toHaveBeenCalled();
    expect(window.localStorage.getItem(key)).toBeNull();
  });
});

describe('Emergency stop during restart readiness', () => {
  const phases = ['checking', 'locked', 'prepared', 'aborting'] as const;
  const transports = ['native', 'browser'] as const;
  const blocked = ['start_chat', 'resume', 'cancel', 'pause', 'settings', 'browser_native_navigate', 'performance_tune', 'native_emergency_stop', 'emergency_stop_extra'];
  async function guarded(phase: typeof phases[number]) {
    const {api} = await import('./api');
    const {updateReadiness} = await import('./updateReadiness');
    expect(updateReadiness.start('synthetic-stop-check', vi.fn())).toBe(true);
    if (phase === 'locked' || phase === 'prepared') {
      expect((await updateReadiness.confirm('synthetic-stop-check')).ok).toBe(true);
      if (phase === 'prepared') expect(updateReadiness.prepared('synthetic-stop-check', updateReadiness.getSnapshot().revision)).toBe(true);
    } else if (phase === 'aborting') updateReadiness.requestAbort();
    expect(updateReadiness.getSnapshot().phase).toBe(phase);
    return {api, updateReadiness};
  }

  for (const transport of transports) {
    it.each(phases)(`admits only the exact stop over ${transport} while %s, accounting for it until acknowledgment`, async phase => {
      let acknowledge!: (value: unknown) => void;
      const pending = new Promise(resolve => {acknowledge = resolve;});
      const call = vi.fn(() => pending);
      const fetch = vi.fn(() => pending);
      vi.stubGlobal('fetch', fetch);
      if (transport === 'native') window.pywebview = {api: {call}};
      const {api, updateReadiness} = await guarded(phase);
      const stopped = api('emergency_stop');
      expect(transport === 'native' ? call : fetch).toHaveBeenCalledTimes(1);
      expect(updateReadiness.getSnapshot().phase).toBe('aborting');
      expect(updateReadiness.canEdit()).toBe(false);
      expect(updateReadiness.diagnose().participants.find(item => item.kind === 'requests')?.pending).toBe(1);
      for (const action of blocked) await expect(api(action, action === 'settings' ? {model: 'synthetic'} : {})).rejects.toThrow('confirming a restart');
      expect(transport === 'native' ? call : fetch).toHaveBeenCalledTimes(1);
      if (transport === 'native') {
        expect(call).toHaveBeenCalledWith('emergency_stop', {});
        acknowledge({ok: true});
      } else {
        expect(fetch).toHaveBeenCalledWith('/api/v1/emergency_stop', expect.objectContaining({method: 'POST', body: '{}', credentials: 'same-origin', redirect: 'error'}));
        acknowledge(response({ok: true}));
      }
      await expect(stopped).resolves.toEqual({ok: true});
      expect(updateReadiness.diagnose().participants.find(item => item.kind === 'requests')?.pending).toBe(0);
      // A stop acknowledgment is not permission to resume editing or proof
      // that the native restart helper has finished aborting.
      expect(updateReadiness.getSnapshot().phase).toBe('aborting');
      await expect(api('resume', {id: 'synthetic-run'})).rejects.toThrow('confirming a restart');
    });

    it.each([null, {}, {ok: false}, {ok: 'true'}])(`does not manufacture an emergency-stop acknowledgment over ${transport} from %j`, async body => {
      const call = vi.fn(async () => body);
      vi.stubGlobal('fetch', vi.fn(async () => response(body)));
      if (transport === 'native') window.pywebview = {api: {call}};
      const {api, updateReadiness} = await guarded('prepared');
      await expect(api('emergency_stop')).rejects.toThrow('The emergency stop could not be confirmed');
      expect(updateReadiness.diagnose().participants.find(item => item.kind === 'requests')?.pending).toBe(0);
      expect(updateReadiness.canEdit()).toBe(false);
    });

    it(`preserves stop errors over ${transport} and releases only its pending counter`, async () => {
      const call = vi.fn(async () => ({error: 'Synthetic stop refusal'}));
      vi.stubGlobal('fetch', vi.fn(async () => response({error: 'Synthetic stop refusal'}, false, 503)));
      if (transport === 'native') window.pywebview = {api: {call}};
      const {api, updateReadiness} = await guarded('locked');
      await expect(api('emergency_stop')).rejects.toThrow('Synthetic stop refusal');
      expect(updateReadiness.diagnose().participants.find(item => item.kind === 'requests')?.pending).toBe(0);
      expect(updateReadiness.canEdit()).toBe(false);
    });
  }

  it('keeps a rejected transport honest without unlocking a checked restart', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('Synthetic connection failure')));
    const {api, updateReadiness} = await guarded('checking');
    await expect(api('emergency_stop')).rejects.toThrow('Synthetic connection failure');
    expect(updateReadiness.diagnose().participants.find(item => item.kind === 'requests')?.pending).toBe(0);
    expect(updateReadiness.canEdit()).toBe(false);
  });

  it.each(transports)('reads desktop evidence over %s without a mutation or readiness unlock', async transport => {
    const call = vi.fn(async () => ({snapshot: null}));
    const fetch = vi.fn(async () => response({snapshot: null}));
    vi.stubGlobal('fetch', fetch);
    if (transport === 'native') window.pywebview = {api: {call}};
    const {api, updateReadiness} = await guarded('prepared');
    await expect(api('computer_view_state', {chat_id: 'synthetic-chat'})).resolves.toEqual({snapshot: null});
    expect(updateReadiness.getSnapshot().phase).toBe('prepared');
    expect(updateReadiness.diagnose().participants.find(item => item.kind === 'requests')?.pending).toBe(0);
    await expect(api('computer_view_state_extra')).rejects.toThrow('confirming a restart');
    expect(transport === 'native' ? call : fetch).toHaveBeenCalledTimes(1);
  });
});
