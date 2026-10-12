import { useEffect, useState } from 'react';
import { api, isNative } from './api';

interface HostVisibility {visible:boolean;visibility_seq:number;}

/** WebView2 can keep document.visibilityState visible after its native form hides. */
export function useWorkspaceVisibility():boolean {
  const [documentVisible,setDocumentVisible]=useState(()=>document.visibilityState!=='hidden');
  const [hostVisible,setHostVisible]=useState(true);
  useEffect(()=>{
    let mounted=true, sequence=-1, requestGeneration=0;
    const accept=(visible:unknown,seq:unknown)=>{
      if(!mounted || typeof visible!=='boolean' || typeof seq!=='number' || !Number.isSafeInteger(seq) || seq<0 || seq<=sequence)return;
      sequence=seq;setHostVisible(visible);
    };
    const documentChanged=()=>setDocumentVisible(document.visibilityState!=='hidden');
    const nativeChanged=(event:Event)=>{if(!isNative())return;const detail=(event as CustomEvent<{visible?:unknown;seq?:unknown}>).detail;if(detail)accept(detail.visible,detail.seq);};
    const nativeReady=()=>{
      if(!isNative())return;const generation=++requestGeneration;
      void api<HostVisibility>('host_status').then(status=>{if(mounted && generation===requestGeneration && isNative())accept(status?.visible,status?.visibility_seq);}).catch(()=>{/* Older hosts still follow document visibility until native events arrive. */});
    };
    document.addEventListener('visibilitychange',documentChanged);
    window.addEventListener('forge:visibility',nativeChanged);
    window.addEventListener('pywebviewready',nativeReady);
    nativeReady();
    return()=>{mounted=false;requestGeneration++;document.removeEventListener('visibilitychange',documentChanged);window.removeEventListener('forge:visibility',nativeChanged);window.removeEventListener('pywebviewready',nativeReady);};
  },[]);
  return documentVisible && (!isNative() || hostVisible);
}
