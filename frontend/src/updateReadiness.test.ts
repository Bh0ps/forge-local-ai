import { describe, expect, it, vi } from 'vitest';
import { ReadinessRegistry, installReadinessFence, updateReadiness, type ReadinessParticipant } from './updateReadiness';
const deferred=<T>()=>{let resolve!:(value:T)=>void;const promise=new Promise<T>(done=>{resolve=done;});return {promise,resolve};};
const ready=():ReadinessParticipant=>({inspect:()=>({reasons:[]}),flushAndConfirm:async()=>({ok:true,receipts:[]})});
describe('content-free restart readiness',()=>{
  it('confirms a stable owner and requires identical revision and membership',async()=>{
    const registry=new ReadinessRegistry();registry.register(ready());registry.start('attempt',vi.fn());
    const result=await registry.confirm('attempt');expect(result.ok).toBe(true);expect(result.participantCount).toBe(1);
    expect(registry.fence('attempt',result.revision!,1)).toBe(true);expect(registry.fence('other',result.revision!,1)).toBe(false);expect(registry.fence('attempt',result.revision!,0)).toBe(false);
  });
  it.each(['draft_conflict','draft_restoring','draft_save_failed','draft_references','unsent_images','dictation_pending','submission_unknown','dialog_open','builder_scratch'] as const)('refuses %s without requesting save or exposing content',async reason=>{
    const registry=new ReadinessRegistry(),flush=vi.fn();registry.register({inspect:()=>({reasons:[reason]}),flushAndConfirm:flush});registry.start('attempt',vi.fn());
    expect(await registry.confirm('attempt')).toEqual({ok:false,reason});expect(flush).not.toHaveBeenCalled();expect(JSON.stringify(registry.getSnapshot())).not.toMatch(/text|secret|scope|path|fingerprint/);
  });
  it('retains an unmounted owner until its pending work is confirmed',async()=>{
    const registry=new ReadinessRegistry();let pending=true;const handle=registry.register({...ready(),retain:()=>pending});handle.detach();expect(registry.participantCount()).toBe(1);
    pending=false;handle.settled();expect(registry.participantCount()).toBe(0);
  });
  it('keeps an already detached draft in the first attempt when its expected flush completes',async()=>{
    const registry=new ReadinessRegistry();let dirty=true;let handle:ReturnType<ReadinessRegistry['register']>;
    handle=registry.register({...ready(),retain:()=>dirty,flushAndConfirm:async()=>{dirty=false;handle.settled();return {ok:true,receipts:['d'.repeat(64)]};}});handle.detach();
    registry.start('attempt',vi.fn());const confirmed=await registry.confirm('attempt');expect(confirmed.ok).toBe(true);expect(confirmed.participantCount).toBe(1);expect(registry.fence('attempt',confirmed.revision!,1)).toBe(true);
    registry.aborted('attempt');expect(registry.participantCount()).toBe(0);
  });
  it('invalidates late work during a deferred save and keeps editing locked until abort acknowledgment',async()=>{
    const registry=new ReadinessRegistry(),save=deferred<{ok:true;receipts:string[]}>(),abort=vi.fn();const handle=registry.register({...ready(),flushAndConfirm:()=>save.promise});registry.start('attempt',abort);
    const checking=registry.confirm('attempt');handle.changed();expect(abort).toHaveBeenCalledOnce();expect(registry.canEdit()).toBe(false);
    save.resolve({ok:true,receipts:[]});expect(await checking).toEqual({ok:false,reason:'workspace_changed'});
    expect(registry.aborted('old')).toBe(false);expect(registry.canEdit()).toBe(false);expect(registry.aborted('attempt','workspace_changed')).toBe(true);expect(registry.canEdit()).toBe(true);
  });
  it('invalidates a prepared fence when an owner appears or disappears',async()=>{
    const registry=new ReadinessRegistry();const handle=registry.register(ready());registry.start('attempt',vi.fn());const result=await registry.confirm('attempt');registry.prepared('attempt',result.revision!);
    handle.detach();expect(registry.fence('attempt',result.revision!,1)).toBe(false);expect(registry.getSnapshot().phase).toBe('aborting');
  });
  it('returns a bounded expiry while an unfinished operation remains retained',async()=>{
    vi.useFakeTimers();const registry=new ReadinessRegistry(),pending=deferred<{ok:true;receipts:string[]}>();registry.register({...ready(),retain:()=>true,flushAndConfirm:()=>pending.promise});registry.start('attempt',vi.fn());
    const checking=registry.confirm('attempt',50);await vi.advanceTimersByTimeAsync(50);expect(await checking).toEqual({ok:false,reason:'confirmation_expired'});expect(registry.canEdit()).toBe(false);expect(registry.participantCount()).toBe(1);pending.resolve({ok:true,receipts:[]});vi.useRealTimers();
  });
  it('rejects malformed receipts and owner errors without echoing exceptions',async()=>{
    const registry=new ReadinessRegistry();registry.register({...ready(),flushAndConfirm:async()=>{throw new Error('synthetic-private-secret');}});registry.start('attempt',vi.fn());expect(await registry.confirm('attempt')).toEqual({ok:false,reason:'draft_save_failed'});
  });
});
describe('bounded readiness diagnostics',()=>{
  it('returns only allowlisted metadata, without private payloads or save callbacks',()=>{
    const registry=new ReadinessRegistry(),flush=vi.fn(),retain=vi.fn();
    registry.register({kind:'layout',inspect:()=>({reasons:['preferences_pending','preferences_pending','synthetic-private-reason'],state:'load_failed',pending:3,load_started:true,scope:'synthetic-private-scope',receipt:'synthetic-private-receipt',content:'synthetic-private-content'}) as never,flushAndConfirm:flush,retain});
    registry.register({kind:'synthetic-private-kind',inspect:()=>({reasons:['synthetic-private-reason'],state:'synthetic-private-state',pending:'synthetic-private-count',load_started:true})} as unknown as ReadinessParticipant);
    registry.register({kind:'draft',inspect:()=>({reasons:[],state:'ready',load_started:true}),flushAndConfirm:flush});
    const diagnostic=registry.diagnose();
    expect(diagnostic).toEqual({participant_count:3,inspected_count:3,truncated:false,participants:[{kind:'layout',reasons:['preferences_pending'],state:'load_failed',pending:3,load_started:true},{kind:'other',reasons:[]},{kind:'draft',reasons:[],state:'ready'}]});
    expect(JSON.stringify(diagnostic)).not.toContain('synthetic-private');expect(flush).not.toHaveBeenCalled();expect(retain).not.toHaveBeenCalled();
  });
  it.each([NaN,Infinity,-1,0.5,Number.MAX_SAFE_INTEGER+1])('omits invalid pending counts %s',pending=>{
    const registry=new ReadinessRegistry();registry.register({...ready(),inspect:()=>({reasons:[],pending})});expect(registry.diagnose().participants[0]).toEqual({kind:'other',reasons:[]});
  });
  it('caps inspected owners, reason input, and counts without calling omitted owners',()=>{
    const registry=new ReadinessRegistry(),inspect=vi.fn(()=>({reasons:[],pending:Number.MAX_SAFE_INTEGER})),omitted=vi.fn(()=>({reasons:[]}));
    for(let index=0;index<512;index++)registry.register({...ready(),kind:'requests',inspect});registry.register({...ready(),inspect:omitted});
    const diagnostic=registry.diagnose();expect(diagnostic.participant_count).toBe(512);expect(diagnostic.inspected_count).toBe(512);expect(diagnostic.truncated).toBe(true);expect(diagnostic.participants).toHaveLength(512);expect(inspect).toHaveBeenCalledTimes(512);expect(omitted).not.toHaveBeenCalled();expect(diagnostic.participants.every(row=>row.pending===512)).toBe(true);
    const bounded=new ReadinessRegistry();bounded.register({...ready(),inspect:()=>({reasons:[...Array(512).fill('private-invalid'), 'preferences_pending']})});expect(bounded.diagnose().participants[0].reasons).toEqual([]);
  });
  it('does not echo inspection or metadata getter exceptions',()=>{
    const registry=new ReadinessRegistry(),flush=vi.fn();registry.register({kind:'workspace',inspect:()=>{throw Error('synthetic-private-exception');},flushAndConfirm:flush});registry.register({get kind():never{throw Error('synthetic-private-getter');},...ready()});
    expect(registry.diagnose().participants).toEqual([{kind:'workspace',reasons:[],state:'waiting'},{kind:'other',reasons:[],state:'waiting'}]);expect(flush).not.toHaveBeenCalled();expect(JSON.stringify(registry.diagnose())).not.toContain('synthetic-private');
  });
  it('captures changing metadata and reason getters once before validation',()=>{
    const reads={state:0,pending:0,load:0,reasons:0,reason:0};
    const reasons=[] as string[];Object.defineProperty(reasons,0,{get:()=>++reads.reason===1?'preferences_pending':'synthetic-private-reason'});
    const observation={
      get reasons(){return ++reads.reasons===1?reasons:['synthetic-private-array'];},
      get state(){return ++reads.state<=2?'ready':'synthetic-private-state';},
      get pending(){return ++reads.pending===1?3:'synthetic-private-count';},
      get load_started(){return ++reads.load===1?true:{private:'synthetic-private-load'};},
    };
    const registry=new ReadinessRegistry();registry.register({...ready(),kind:'layout',inspect:()=>observation as never});const diagnostic=registry.diagnose();
    expect(diagnostic.participants).toEqual([{kind:'layout',reasons:['preferences_pending'],state:'ready',pending:3,load_started:true}]);expect(reads).toEqual({state:1,pending:1,load:1,reasons:1,reason:1});expect(JSON.stringify(diagnostic)).not.toContain('synthetic-private');
  });
  it('does not call overridden reason array methods or iterators',()=>{
    const reasons=['preferences_pending','synthetic-private-reason','preferences_pending'];
    const overridden=vi.fn(()=>['synthetic-private-method']);Object.defineProperties(reasons,{slice:{value:overridden},filter:{value:overridden},[Symbol.iterator]:{value:overridden}});
    const registry=new ReadinessRegistry();registry.register({...ready(),inspect:()=>({reasons}) as never});const diagnostic=registry.diagnose();expect(diagnostic.participants).toEqual([{kind:'other',reasons:['preferences_pending']}]);expect(overridden).not.toHaveBeenCalled();expect(JSON.stringify(diagnostic)).not.toContain('synthetic-private');
  });
  it('cannot change a checking or prepared authority, membership, or revision',async()=>{
    const registry=new ReadinessRegistry(),inspect=vi.fn(()=>({reasons:[] as const,state:'ready' as const})),flush=vi.fn(async()=>({ok:true as const,receipts:[]}));registry.register({kind:'draft',inspect,flushAndConfirm:flush});
    const invalidated=vi.fn(),changed=vi.fn();registry.subscribe(changed);registry.start('synthetic-private-attempt',invalidated);const checking=registry.getSnapshot();changed.mockClear();
    const diagnostic=registry.diagnose();expect(registry.getSnapshot()).toBe(checking);expect(changed).not.toHaveBeenCalled();expect(invalidated).not.toHaveBeenCalled();expect(flush).not.toHaveBeenCalled();expect(registry.participantCount()).toBe(1);expect(registry.canEdit()).toBe(false);expect(JSON.stringify(diagnostic)).not.toContain('synthetic-private-attempt');
    const confirmed=await registry.confirm('synthetic-private-attempt');registry.prepared('synthetic-private-attempt',confirmed.revision!);const prepared=registry.getSnapshot();changed.mockClear();registry.diagnose();expect(registry.getSnapshot()).toBe(prepared);expect(changed).not.toHaveBeenCalled();expect(registry.fence('synthetic-private-attempt',confirmed.revision!,1)).toBe(true);expect(flush).toHaveBeenCalledOnce();
  });
  it('exposes diagnosis separately while preserving the exact native fence key set',()=>{
    const stop=installReadinessFence();try{
      const before=updateReadiness.getSnapshot(),check=window.__forgeUpdateReadiness!.check({attempt_id:'synthetic-private-attempt',workspace_revision:0,participant_count:0});
      expect(Object.keys(check).sort()).toEqual(['ok','participant_count','phase','workspace_revision']);const diagnostic=window.__forgeUpdateReadiness!.diagnose();expect(Object.keys(diagnostic).sort()).toEqual(['inspected_count','participant_count','participants','truncated']);expect(updateReadiness.getSnapshot()).toBe(before);expect(window.__forgeUpdateReadiness!.check({attempt_id:'synthetic-private-attempt',workspace_revision:0,participant_count:0})).toEqual(check);
    }finally{stop();}expect(window.__forgeUpdateReadiness).toBeUndefined();
  });
});
