import { useLayoutEffect, useRef, useState } from 'react';
import { updateReadiness, type ReadinessReason } from './updateReadiness';

interface ScratchOwner<T> { value:T; initial:T; handle:ReturnType<typeof updateReadiness.register>; mounted:number; }
// Scratch content belongs to this private owner, never the readiness registry.
const owners=new Map<string,ScratchOwner<unknown>>();
function ownerFor<T>(key:string,initial:T,reason:ReadinessReason) {
  let owner=owners.get(key) as ScratchOwner<T>|undefined;
  if(!owner){
    owner={value:initial,initial,mounted:0,handle:null as unknown as ScratchOwner<T>['handle']};
    const captured=owner,dirty=()=>JSON.stringify(captured.value)!==JSON.stringify(captured.initial);
    owner.handle=updateReadiness.register({inspect:()=>({reasons:dirty()?[reason]:[]}),retain:dirty,flushAndConfirm:async()=>dirty()?{ok:false,reason}:{ok:true,receipts:[]}});
    owners.set(key,owner);
  }
  return owner;
}
export function useRetainedScratch<T>(key:string, initial:T, reason:ReadinessReason='builder_scratch') {
  const latest=useRef(initial), [value,setValue]=useState<T>(()=>(owners.get(key)?.value as T) ?? initial);
  const [,refresh]=useState(0);
  latest.current=initial;
  const ownerRef=useRef<ScratchOwner<T>|undefined>(undefined);
  useLayoutEffect(()=>{
    const owner=ownerFor(key,latest.current,reason);
    if(!owner.handle.attach()){const dirty=()=>JSON.stringify(owner.value)!==JSON.stringify(owner.initial);owner.handle=updateReadiness.register({inspect:()=>({reasons:dirty()?[reason]:[]}),retain:dirty,flushAndConfirm:async()=>dirty()?{ok:false,reason}:{ok:true,receipts:[]}});}owner.mounted++;ownerRef.current=owner;setValue(owner.value);refresh(version=>version+1);
    return ()=>{owner.mounted--;if(!owner.mounted){owner.handle.detach();if(JSON.stringify(owner.value)===JSON.stringify(owner.initial))owners.delete(key);}ownerRef.current=undefined;};
  },[key]);
  const update=(next:T | ((previous:T)=>T))=>{
    if(!updateReadiness.canEdit())return;
    const owner=owners.get(key) as ScratchOwner<T>|undefined;if(!owner)return;
    owner.handle.changed();owner.value=typeof next==='function' ? (next as (previous:T)=>T)(owner.value) : next;setValue(owner.value);
  };
  const apply=(next:T | ((previous:T)=>T))=>{const owner=ownerFor(key,initial,reason);owner.handle.changed();owner.value=typeof next==='function'?(next as (previous:T)=>T)(owner.value):next;if(ownerRef.current===owner)setValue(owner.value);if(!owner.mounted)owner.handle.detach();};
  const discard=()=>{const owner=owners.get(key) as ScratchOwner<T>|undefined;if(!owner || !updateReadiness.canEdit())return;owner.handle.changed();owner.value=owner.initial;setValue(owner.value);};
  const accepted=(captured?:T)=>{const owner=owners.get(key) as ScratchOwner<T>|undefined;if(!owner)return;owner.handle.changed();owner.initial=captured===undefined ? owner.value : captured;owner.handle.settled();if(ownerRef.current===owner)refresh(version=>version+1);};
  return {value,update,apply,discard,accepted,current:()=>(owners.get(key)?.value as T) ?? value,dirty:ownerRef.current ? JSON.stringify(value)!==JSON.stringify(ownerRef.current.initial) : false};
}
