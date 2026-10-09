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
