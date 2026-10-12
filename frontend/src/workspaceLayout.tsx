import { useEffect, useRef, useState } from 'react';
import { api, isNative } from './api';

export interface WorkspaceLayout {sidebar_open:boolean;sidebar_width:number;right_panel_open:boolean;right_panel_width:number;right_panel_tab:string;collapsed_project_ids:string[];}
export const DEFAULT_LAYOUT: WorkspaceLayout = {sidebar_open:true,sidebar_width:260,right_panel_open:false,right_panel_width:320,right_panel_tab:'activity',collapsed_project_ids:[]};
const tabs=['context','browser','activity','goals','files'];
export function normalizeLayout(value:Partial<WorkspaceLayout>):WorkspaceLayout {
  const width=(candidate:unknown,fallback:number,min:number,max:number)=>typeof candidate==='number'&&Number.isFinite(candidate)?Math.round(Math.max(min,Math.min(max,candidate))):fallback;
  return {sidebar_open:typeof value.sidebar_open==='boolean'?value.sidebar_open:true,sidebar_width:width(value.sidebar_width,260,220,360),right_panel_open:typeof value.right_panel_open==='boolean'?value.right_panel_open:false,right_panel_width:width(value.right_panel_width,320,280,520),right_panel_tab:tabs.includes(value.right_panel_tab || '')?value.right_panel_tab!:'activity',collapsed_project_ids:Array.isArray(value.collapsed_project_ids)?value.collapsed_project_ids.filter(id=>typeof id==='string'):[]};
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
  const touched=useRef(false), latest=useRef(layout), lastSaved=useRef(''), client=useRef(''), queue=useRef(Promise.resolve()), mounted=useRef(true);
  latest.current=layout;
  useEffect(()=>{mounted.current=true;const nativeReady=()=>{if(client.current && client.current!==layoutClientId())setTransportRevision(value=>value+1);};window.addEventListener('pywebviewready',nativeReady);return()=>{mounted.current=false;window.removeEventListener('pywebviewready',nativeReady);};},[]);
  useEffect(()=>{if(!enabled)return;let active=true;client.current=layoutClientId();void api<WorkspaceLayout>('ui_layout_get',{client_id:client.current}).then(saved=>{if(!active)return;const next=normalizeLayout(saved || {});lastSaved.current=JSON.stringify(next);if(!touched.current)setLayout(next);setReady(true);}).catch(()=>{if(active){lastSaved.current=JSON.stringify(DEFAULT_LAYOUT);setReady(true);}});return()=>{active=false;};},[enabled,transportRevision]);
  const save=(captured:WorkspaceLayout)=>{const encoded=JSON.stringify(captured);if(encoded===lastSaved.current)return;queue.current=queue.current.catch(()=>{}).then(async()=>{if(JSON.stringify(latest.current)!==encoded)return;try{await api('ui_layout_save',{client_id:client.current,layout:captured});lastSaved.current=encoded;}catch{if(mounted.current)notify('Workspace layout could not be saved.');}});};
  useEffect(()=>{if(!ready)return;const timer=setTimeout(()=>save(layout),300);return()=>clearTimeout(timer);},[layout,ready]);
  useEffect(()=>()=>{if(client.current && touched.current)save(latest.current);},[]);
  const update=(delta:Partial<WorkspaceLayout>)=>{touched.current=true;setLayout(previous=>normalizeLayout({...previous,...delta}));};
  return {layout,update,ready};
}
export function ResizeHandle({label,value,min,max,change,reverse=false}: {label:string;value:number;min:number;max:number;change:(value:number)=>void;reverse?:boolean}) {
  const drag=useRef<{x:number;width:number;pointer:number}|null>(null);
  return <div className="pane-resize-handle" role="separator" aria-label={label} aria-orientation="vertical" aria-valuemin={min} aria-valuemax={max} aria-valuenow={Math.round(value)} tabIndex={0} onKeyDown={event=>{if(event.key==='ArrowLeft'||event.key==='ArrowRight'){event.preventDefault();change(Math.round(Math.max(min,Math.min(max,value+(event.key==='ArrowRight'?1:-1)*(reverse?-1:1)*10))));}else if(event.key==='Home'){event.preventDefault();change(min);}else if(event.key==='End'){event.preventDefault();change(max);}}} onPointerDown={event=>{if(event.button!==0)return;drag.current={x:event.clientX,width:value,pointer:event.pointerId};event.currentTarget.setPointerCapture(event.pointerId);event.preventDefault();}} onPointerMove={event=>{if(!drag.current || drag.current.pointer!==event.pointerId)return;change(Math.round(Math.max(min,Math.min(max,drag.current.width+(event.clientX-drag.current.x)*(reverse?-1:1)))));}} onPointerUp={event=>{drag.current=null;if(event.currentTarget.hasPointerCapture(event.pointerId))event.currentTarget.releasePointerCapture(event.pointerId);}} onPointerCancel={()=>{drag.current=null;}}/ >;
}
