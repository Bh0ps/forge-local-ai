import { useEffect, useState } from 'react';
import { api } from './api';
import type { Settings } from './types';

export const contextBands=[{name:'Low',tokens:2048,accent:'sage',description:'Short exchanges'},{name:'Medium',tokens:8192,accent:'blue',description:'Small changes and research'},{name:'High',tokens:32768,accent:'copper',description:'Balanced project work'},{name:'Max',tokens:131072,accent:'violet',description:'Large files and longer tasks'},{name:'Ultra',tokens:262144,accent:'rose',description:'Largest window; more memory'}] as const;
export function contextBand(tokens:number){return contextBands.find(band=>tokens<=band.tokens)||contextBands[4];}
export function capacityLabel(tokens:number){return `${new Intl.NumberFormat('en').format(tokens)} tokens configured`;}
export interface ContextMetadata {limit:number|null;values:unknown[];mandatory:boolean;known:boolean;varies?:boolean;controlSupported?:boolean;maximum?:number;instances?:{model:string;context_length:number|null}[];limitSource?:string;}
const unknown:ContextMetadata={limit:null,values:[],mandatory:false,known:false};
export function useContextMetadata(settings:Settings) {
  const bridge=window.pywebview?.api;
  const [metadata,setMetadata]=useState(unknown),[retry,setRetry]=useState(0),[failed,setFailed]=useState(false);
  useEffect(()=>{
    let current=true;setMetadata(unknown);setFailed(false);
    if(settings.model)void api<Record<string,unknown>>('show',{model:settings.model,provider_id:settings.provider_id||'ollama'}).then(result=>{
      if(!current || window.pywebview?.api!==bridge)return;
      const info=result.model_info as Record<string,unknown>|undefined;
      const raw=result.context_limit||result.context_length||result.max_context||Object.entries(info||{}).find(([key])=>key.endsWith('.context_length'))?.[1];
      const thinking=result.thinking as {values?:unknown[]}|undefined,reasoning=result.reasoning as {mandatory?:unknown;control_supported?:unknown}|undefined;
      const advertised=result.advertised_metadata as {max_context_length?:unknown;loaded_instances?:{model:string;context_length:number|null}[]}|undefined;
      const values=Array.isArray(thinking?.values)?thinking.values:[];
      setMetadata({limit:typeof raw==='number' && Number.isSafeInteger(raw) && raw>0?raw:null,values,mandatory:reasoning?.mandatory===true,controlSupported:typeof reasoning?.control_supported==='boolean'?reasoning.control_supported:undefined,known:values.length>0 || typeof reasoning?.control_supported==='boolean' || reasoning?.mandatory===true || Array.isArray(result.capabilities) && result.capabilities.includes('thinking'),maximum:typeof advertised?.max_context_length==='number' && Number.isSafeInteger(advertised.max_context_length) && advertised.max_context_length>0?advertised.max_context_length:undefined,instances:Array.isArray(advertised?.loaded_instances)?advertised.loaded_instances.slice(0,16):undefined,limitSource:result.context_limit_source==='configured' || result.context_limit_source==='loaded_instance'?result.context_limit_source:undefined});
    }).catch(()=>{if(current && window.pywebview?.api===bridge)setFailed(true);});
    return()=>{current=false;};
  },[settings.model,settings.provider_id,retry,bridge]);
  return {metadata:{...metadata,varies:settings.provider_id==='openrouter' && ['openrouter/free','openrouter/auto'].includes(settings.model)},failed,retry:()=>setRetry(value=>value+1)};
}
export function reasoningLabel(thinking:boolean,metadata:ContextMetadata){
  if(metadata.varies)return `Reasoning: Varies by route · requested ${thinking?'On':'Off'}`;
  if(metadata.mandatory || metadata.values.length>0 && !metadata.values.includes(false) && metadata.values.some(value=>value===true || ['low','medium','high'].includes(String(value))))return 'Reasoning: Required';
  if(metadata.controlSupported===false)return 'Reasoning: Engine controlled';
  if(metadata.values.length===1 && metadata.values[0]===false)return 'Reasoning: Unavailable';
  return `Reasoning requested: ${thinking?'On':'Off'}${metadata.known?'':' · support unreported'}`;
}
