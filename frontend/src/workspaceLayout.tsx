import { useEffect, useRef, useState } from 'react';
import { api, isNative } from './api';
import { updateReadiness, useReadinessParticipant } from './updateReadiness';

export interface WorkspaceLayout {sidebar_open:boolean;sidebar_width:number;right_panel_open:boolean;right_panel_width:number;right_panel_tab:string;collapsed_project_ids:string[];}
export const DEFAULT_LAYOUT: WorkspaceLayout = {sidebar_open:true,sidebar_width:260,right_panel_open:false,right_panel_width:320,right_panel_tab:'activity',collapsed_project_ids:[]};
const tabs=['context','browser','activity','goals','files'];
export function normalizeLayout(value:Partial<WorkspaceLayout>):WorkspaceLayout {
  const width=(candidate:unknown,fallback:number,min:number,max:number)=>typeof candidate==='number'&&Number.isFinite(candidate)?Math.round(Math.max(min,Math.min(max,candidate))):fallback;
  return {sidebar_open:typeof value.sidebar_open==='boolean'?value.sidebar_open:true,sidebar_width:width(value.sidebar_width,260,220,360),right_panel_open:typeof value.right_panel_open==='boolean'?value.right_panel_open:false,right_panel_width:width(value.right_panel_width,320,280,520),right_panel_tab:tabs.includes(value.right_panel_tab || '')?value.right_panel_tab!:'activity',collapsed_project_ids:Array.isArray(value.collapsed_project_ids)?value.collapsed_project_ids.filter(id=>typeof id==='string'):[]};
}
function acknowledgedLayout(value:unknown):WorkspaceLayout|null {
  if(!value || typeof value!=='object' || Array.isArray(value))return null;
  const saved=value as Partial<WorkspaceLayout>;
  if(typeof saved.sidebar_open!=='boolean' || typeof saved.right_panel_open!=='boolean'
    || typeof saved.sidebar_width!=='number' || !Number.isFinite(saved.sidebar_width)
    || typeof saved.right_panel_width!=='number' || !Number.isFinite(saved.right_panel_width)
    || !tabs.includes(saved.right_panel_tab || '') || !Array.isArray(saved.collapsed_project_ids)
    || saved.collapsed_project_ids.some(id=>typeof id!=='string'))return null;
  return normalizeLayout(saved);
}
let browserClient='';
export function layoutClientId() {
  if(isNative())return 'desktop';
  if(browserClient)return browserClient;
  try {const saved=localStorage.getItem('forge.ui-client.v1');if(saved && /^browser-[0-9a-f-]{36}$/.test(saved))return browserClient=saved;}catch{/* Storage may be restricted. */}
  browserClient=`browser-${crypto.randomUUID()}`;
  try{localStorage.setItem('forge.ui-client.v1',browserClient);}catch{/* This tab retains its identity. */}
  return browserClient;
}
export function useWorkspaceLayout(enabled:boolean,notify:(message:string)=>void) {
  const [layout,setLayout]=useState<WorkspaceLayout>(DEFAULT_LAYOUT), [ready,setReady]=useState(false), [transportRevision,setTransportRevision]=useState(0);
  const [error,setError]=useState<'load'|'save'|null>(null), [retrying,setRetrying]=useState(false);
  const touched=useRef(false), latest=useRef(layout), lastSaved=useRef(''), client=useRef(''), queue=useRef(Promise.resolve()), mounted=useRef(true);
  const scopeGeneration=useRef(0), loadRequest=useRef(0), enabledRef=useRef(enabled), readyRef=useRef(false), loadStarted=useRef(false);
  const errorRef=useRef<'load'|'save'|null>(null), retryingRef=useRef(false);
  enabledRef.current=enabled;
  latest.current=layout;
  const current=(identity:string,generation:number)=>enabledRef.current && identity===client.current && identity===layoutClientId() && generation===scopeGeneration.current;
  const failure=(value:'load'|'save'|null)=>{errorRef.current=value;if(mounted.current)setError(value);};
  const loaded=(value:boolean)=>{readyRef.current=value;if(mounted.current)setReady(value);};

  // Writes to one client row stay on one queue. Replies can update only the
  // identity that admitted them; a browser request cannot poison desktop state.
  const save=(captured:WorkspaceLayout,force=false,useLatest=false):Promise<boolean>=>{
    const identity=client.current,generation=scopeGeneration.current;
    if(!readyRef.current || !current(identity,generation))return Promise.resolve(false);
    const pending=queue.current.then(async()=>{
      if(!readyRef.current || !current(identity,generation))return false;
      const value=useLatest?latest.current:captured,encoded=JSON.stringify(value);
      if(encoded!==JSON.stringify(latest.current))return false;
      if(!force && encoded===lastSaved.current)return !errorRef.current;
      try {
        const response=await api<unknown>('ui_layout_save',{client_id:identity,layout:value});
        if(!current(identity,generation))return false;
        const saved=acknowledgedLayout(response);
        // The coordinator may remove collapsed IDs for deleted projects. It
        // must still acknowledge every other preference from this write.
        if(!saved || saved.collapsed_project_ids.some(id=>!value.collapsed_project_ids.includes(id))
          || JSON.stringify({...saved,collapsed_project_ids:value.collapsed_project_ids})!==encoded)throw new Error('Invalid layout acknowledgement');
        lastSaved.current=JSON.stringify(saved);
        if(encoded!==JSON.stringify(latest.current))return false;
        latest.current=saved;
        if(mounted.current)setLayout(saved);
        failure(null);
        return true;
      } catch {
        if(current(identity,generation)){
          failure('save');
          if(mounted.current)notify('Workspace layout could not be saved.');
        }
        return false;
      }
    });
    queue.current=pending.then(()=>{},()=>{});
    return pending;
  };
  const load=async(identity:string,generation:number):Promise<boolean>=>{
    const request=++loadRequest.current;
    const owns=()=>mounted.current && current(identity,generation) && request===loadRequest.current;
    try {
      loadStarted.current=true;
      const response=await api<unknown>('ui_layout_get',{client_id:identity});
      if(!owns())return false;
      const next=acknowledgedLayout(response);
      if(!next)throw new Error('Invalid layout response');
      lastSaved.current=JSON.stringify(next);
      if(!touched.current){latest.current=next;setLayout(next);}
      failure(null);
      loaded(true);
      return true;
    } catch {
      if(owns()){
        failure('load');loaded(false);
        notify('Workspace layout could not be loaded. Retry before restarting.');
      }
      return false;
    }
  };
  const retry=async():Promise<boolean>=>{
    const identity=client.current,generation=scopeGeneration.current,kind=errorRef.current;
    if(!mounted.current || !current(identity,generation) || !kind || retryingRef.current || !updateReadiness.canEdit())return false;
    retryingRef.current=true;setRetrying(true);
    try {
      if(kind==='load' && !await load(identity,generation))return false;
      if(!current(identity,generation))return false;
      // A failed write requires a real acknowledgement, even if the user has
      // restored the previous value. A successful read preserves local edits.
      if(kind==='save' || JSON.stringify(latest.current)!==lastSaved.current){
        if(!await save(latest.current,true,true))return false;
      }
      return mounted.current && current(identity,generation) && readyRef.current && !errorRef.current && JSON.stringify(latest.current)===lastSaved.current;
    } finally {
      if(current(identity,generation)){
        retryingRef.current=false;
        if(mounted.current)setRetrying(false);
      }
    }
  };
  useReadinessParticipant({
    kind:'layout',
    inspect:()=>({reasons:enabled && (!readyRef.current || errorRef.current || retryingRef.current)?['preferences_pending']:[],
      state:!enabled?'disabled':retryingRef.current?'retrying':errorRef.current==='load'?'load_failed':errorRef.current==='save'?'save_failed':!client.current?'uninitialized':!readyRef.current?'loading':'ready',
      load_started:loadStarted.current,
    }),
    flushAndConfirm:async()=>{
      const identity=client.current,generation=scopeGeneration.current;
      if(!readyRef.current || errorRef.current || retryingRef.current)return {ok:false,reason:'preferences_pending'};
      const confirmed=await save(latest.current);
      return confirmed && current(identity,generation) && !errorRef.current && JSON.stringify(latest.current)===lastSaved.current
        ?{ok:true,receipts:[]}:{ok:false,reason:'preferences_pending'};
    },
  });
  useEffect(()=>{
    mounted.current=true;
    const nativeReady=()=>{if(client.current && client.current!==layoutClientId())setTransportRevision(value=>value+1);};
    window.addEventListener('pywebviewready',nativeReady);
    return()=>{mounted.current=false;loadRequest.current++;window.removeEventListener('pywebviewready',nativeReady);};
  },[]);
  useEffect(()=>{
    if(!enabled)return;
    const identity=layoutClientId(),generation=++scopeGeneration.current;
    if(client.current && client.current!==identity){
      touched.current=false;updateReadiness.changed();
      // Retired requests still finish against their explicit old client_id.
      // They must not hold writes to the new client's separate coordinator row.
      queue.current=Promise.resolve();
    }
    client.current=identity;loaded(false);loadStarted.current=false;lastSaved.current='';failure(null);retryingRef.current=false;setRetrying(false);
    void load(identity,generation);
    return()=>{loadRequest.current++;};
  },[enabled,transportRevision]);
  useEffect(()=>{
    if(!ready || error || retrying)return;
    const timer=setTimeout(()=>void save(layout),300);
    return()=>clearTimeout(timer);
  },[layout,ready,error,retrying]);
  useEffect(()=>()=>{if(client.current && touched.current)void save(latest.current);},[]);
  const update=(delta:Partial<WorkspaceLayout>)=>{
    if(!updateReadiness.canEdit())return;
    updateReadiness.changed();touched.current=true;
    const next=normalizeLayout({...latest.current,...delta});latest.current=next;setLayout(next);
  };
  return {layout,update,ready,error,retrying,retry};
}
export function ResizeHandle({label,value,min,max,change,reverse=false}: {label:string;value:number;min:number;max:number;change:(value:number)=>void;reverse?:boolean}) {
  const drag=useRef<{x:number;width:number;pointer:number}|null>(null);
  return <div className="pane-resize-handle" role="separator" aria-label={label} aria-orientation="vertical" aria-valuemin={min} aria-valuemax={max} aria-valuenow={Math.round(value)} tabIndex={0} onKeyDown={event=>{if(event.key==='ArrowLeft'||event.key==='ArrowRight'){event.preventDefault();change(Math.round(Math.max(min,Math.min(max,value+(event.key==='ArrowRight'?1:-1)*(reverse?-1:1)*10))));}else if(event.key==='Home'){event.preventDefault();change(min);}else if(event.key==='End'){event.preventDefault();change(max);}}} onPointerDown={event=>{if(event.button!==0)return;drag.current={x:event.clientX,width:value,pointer:event.pointerId};event.currentTarget.setPointerCapture(event.pointerId);event.preventDefault();}} onPointerMove={event=>{if(!drag.current || drag.current.pointer!==event.pointerId)return;change(Math.round(Math.max(min,Math.min(max,drag.current.width+(event.clientX-drag.current.x)*(reverse?-1:1)))));}} onPointerUp={event=>{drag.current=null;if(event.currentTarget.hasPointerCapture(event.pointerId))event.currentTarget.releasePointerCapture(event.pointerId);}} onPointerCancel={()=>{drag.current=null;}}/ >;
}
