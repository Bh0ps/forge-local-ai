import { describe, expect, it, vi } from 'vitest';
import { ReadinessRegistry, type ReadinessParticipant } from './updateReadiness';
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
