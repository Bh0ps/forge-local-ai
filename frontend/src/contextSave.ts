import { useLayoutEffect, useMemo, useRef, useSyncExternalStore } from 'react';
import { updateReadiness } from './updateReadiness';
import {api,type NativeBridge} from './api';
import type { Settings } from './types';

interface Snapshot { value:number; confirmed:number; pending:boolean; failed:boolean; reconcile:boolean; }
interface Attachment { save:(value:number)=>Promise<void>; current:()=>boolean; }
type Handle=ReturnType<typeof updateReadiness.register>;
const nativeOwners=new WeakMap<NativeBridge,Map<string,ContextSave>>();
const browserOwners=new Map<string,ContextSave>();
let retainedOwner:ContextSave|undefined;

/** Private retained preference owner. Prop previews never acknowledge a write. */
class ContextSave {
  private snapshot:Snapshot;
  private listeners=new Set<()=>void>();
  private attachments=new Set<Attachment>();
  private handle?:Handle;
  private timer?:ReturnType<typeof setTimeout>;
  private running?:Promise<void>;
  private requiresWrite=false;
  private binding:NativeBridge|string|undefined;
  private bindingRevision=0;
  private intentRevision=0;
  constructor(value:number,private release:()=>void) { this.snapshot={value,confirmed:value,pending:false,failed:false,reconcile:false}; }
  subscribe=(listener:()=>void)=>{this.listeners.add(listener);return()=>{this.listeners.delete(listener);};};
  getSnapshot=()=>this.snapshot;
  bind(binding:NativeBridge|string,confirmed:number){
    if(this.binding===binding)return;
    const changed=this.binding!==undefined;this.binding=binding;this.bindingRevision++;
    if(changed && this.dirty()){
      // Keep prior intent visible. Crossing authorities requires explicit recovery.
      this.requiresWrite=true;if(this.timer)clearTimeout(this.timer);this.timer=undefined;
      this.publish({confirmed,failed:true,reconcile:true});
    }
  }
  private dirty=()=>this.requiresWrite || this.snapshot.value!==this.snapshot.confirmed || this.snapshot.pending || this.snapshot.failed;
  private publish(patch:Partial<Snapshot>) { this.snapshot={...this.snapshot,...patch};this.listeners.forEach(listener=>listener());this.handle?.settled();if(!this.attachments.size && !this.dirty())this.release(); }
  attach(attachment:Attachment) {
    this.attachments.add(attachment);
    if(!this.handle?.attach())this.handle=updateReadiness.register({kind:'other',inspect:()=>({reasons:this.dirty()?['preferences_pending']:[],state:this.snapshot.failed?'save_failed':this.dirty()?'waiting':'ready',pending:this.snapshot.pending?1:0}),retain:this.dirty,flushAndConfirm:async()=>this.dirty()?{ok:false,reason:'preferences_pending'}:{ok:true,receipts:[]}});
    if(this.dirty() && !this.snapshot.failed)queueMicrotask(()=>void this.commit());
    return()=>{this.attachments.delete(attachment);if(!this.attachments.size){if(this.timer)clearTimeout(this.timer);this.timer=undefined;this.handle?.detach();if(!this.dirty())this.release();}};
  }
  acceptExternal(value:number) { if(!this.dirty() && value!==this.snapshot.value)this.publish({value,confirmed:value}); }
  preview=(value:number)=>{
    if(!updateReadiness.canEdit() || !Number.isSafeInteger(value) || value<2048 || value>262144)return;
    this.intentRevision++;this.handle?.changed();this.publish({value,failed:false});
    if(this.timer)clearTimeout(this.timer);
    this.timer=setTimeout(()=>{this.timer=undefined;void this.commit();},250);
  };
  commit=async():Promise<void>=>{
    if(this.timer)clearTimeout(this.timer);this.timer=undefined;
    if(this.running)return this.running;
    if(!updateReadiness.canEdit() || !this.dirty() || this.snapshot.failed)return;
    const attachment=[...this.attachments].reverse().find(item=>item.current());
    if(!attachment)return;
    const value=this.snapshot.value,bindingRevision=this.bindingRevision;this.publish({pending:true});
    const work=(async()=>{
      try { await attachment.save(value);if(bindingRevision===this.bindingRevision){if(this.snapshot.value===value)this.requiresWrite=false;this.publish({confirmed:value,failed:false,reconcile:false});} }
      catch { if(bindingRevision===this.bindingRevision)this.publish({failed:this.snapshot.value===value}); }
      finally { this.running=undefined;this.publish({pending:false}); }
      if(this.snapshot.value!==value && !this.snapshot.failed)await this.commit();
    })();
    this.running=work;return work;
  };
  retry=()=>{if(!updateReadiness.canEdit() || this.snapshot.pending)return;this.publish({failed:false});void this.commit();};
  discard=async()=>{
    if(!updateReadiness.canEdit() || this.snapshot.pending)return;
    const attachment=[...this.attachments].reverse().find(item=>item.current());if(!attachment)return;
    const original=this.snapshot.value,bindingRevision=this.bindingRevision,intentRevision=++this.intentRevision;this.handle?.changed();this.publish({pending:true});
    const work=(async()=>{
      let restored:number|undefined;
      try {
        // A newly attached authority can have different saved preferences.
        const saved=await api<Partial<Settings>>('settings');
        if(bindingRevision===this.bindingRevision && this.intentRevision===intentRevision){
          if(!Number.isSafeInteger(saved?.context) || Number(saved.context)<2048 || Number(saved.context)>262144)throw new Error('Context confirmation unavailable');
          restored=Number(saved.context);this.requiresWrite=true;this.publish({value:restored});
          await attachment.save(restored);
          if(bindingRevision===this.bindingRevision){if(this.snapshot.value===restored)this.requiresWrite=false;this.publish({confirmed:restored,failed:false,reconcile:false});}
        }
      } catch {if(bindingRevision===this.bindingRevision && this.snapshot.value===(restored??original))this.publish({failed:true});}
      finally {this.running=undefined;this.publish({pending:false});}
      if((this.intentRevision!==intentRevision || this.snapshot.value!==(restored??original)) && !this.snapshot.failed)await this.commit();
    })();this.running=work;return work;
  };
}

export function useContextSave(settings:Settings,onChange:(delta:Partial<Settings>)=>Promise<void>) {
  // Context is one workspace preference; model metadata has separate ownership.
  const bridge=window.pywebview?.api,scope='context';
  const owner=useMemo(()=>{
    let owners=browserOwners;
    if(bridge){owners=nativeOwners.get(bridge)||new Map();nativeOwners.set(bridge,owners);}
    let saved=retainedOwner || owners.get(scope);if(!saved){const capturedOwners=owners;saved=new ContextSave(settings.context,()=>{if(capturedOwners.get(scope)===saved)capturedOwners.delete(scope);if(retainedOwner===saved)retainedOwner=undefined;});}owners.set(scope,saved);return saved;
  },[bridge,scope]);
  const latest=useRef({owner,onChange,active:true});latest.current={owner,onChange,active:true};
  useLayoutEffect(()=>{retainedOwner=owner;owner.bind(bridge||window.location.origin,settings.context);latest.current.active=true;const detach=owner.attach({current:()=>latest.current.active && latest.current.owner===owner && window.pywebview?.api===bridge,save:value=>latest.current.onChange({context:value})});return()=>{if(latest.current.owner===owner)latest.current.active=false;detach();};},[owner,bridge]);
  useLayoutEffect(()=>{owner.acceptExternal(settings.context);},[owner,settings.context]);
  const snapshot=useSyncExternalStore(owner.subscribe,owner.getSnapshot);
  return {...snapshot,preview:owner.preview,commit:owner.commit,retry:owner.retry,discard:owner.discard};
}
export type ContextSaveState=ReturnType<typeof useContextSave>;
