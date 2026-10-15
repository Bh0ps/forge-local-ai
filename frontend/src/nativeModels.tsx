import { useEffect, useRef, useState } from 'react';
import { ArrowRight, Check, Cpu, Download, Folder, LoaderCircle, RefreshCw } from 'lucide-react';
import { api, native, bytesLabel, errorText } from './api';
import { Badge, ErrorNotice, Field, IconButton } from './components';

interface NativeModel { id:string; name:string; engine:string; validation:string; files:{size:number}[] }
interface ModelJob { id:string; state:string; message:string; model_id?:string; action?:string; received_bytes?:number;expected_bytes?:number }
interface NativeStatus { state:string; bundled:boolean; catalog:{root:string;models:NativeModel[]}; jobs:ModelJob[] }
interface OllamaModel { id:string;name:string;size:number;has_adapter:boolean }
export function NativeModels({ changed, useModel, selected }: {changed:()=>Promise<void>;useModel:(id:string)=>Promise<void>;selected?:string}) {
  const [status,setStatus]=useState<NativeStatus|null>(null),[error,setError]=useState('');
  const [root,setRoot]=useState(''),[source,setSource]=useState(''),[path,setPath]=useState('');
  const [inventory,setInventory]=useState<OllamaModel[]>([]),[busy,setBusy]=useState('');
  const [advanced,setAdvanced]=useState(false),[loaded,setLoaded]=useState(false);
  const completed=useRef(''), onChanged=useRef(changed);onChanged.current=changed;
  const [refreshVersion,setRefreshVersion]=useState(0);
  useEffect(()=>{
    let live=true, timer:ReturnType<typeof setTimeout>;
    async function refresh(){
      try {
        const value=await api<NativeStatus>('native_status');
        if(!live)return;
        if(!value?.catalog||!Array.isArray(value.catalog.models)||!Array.isArray(value.jobs)||typeof value.catalog.root!=='string')throw new Error('Forge model status is unavailable. Refresh to retry.');
        if(!value.catalog.models.every(model=>model&&typeof model.id==='string'&&typeof model.name==='string'&&Array.isArray(model.files)&&model.files.every(file=>typeof file.size==='number'&&Number.isFinite(file.size)&&file.size>=0)))throw new Error('Forge model metadata is unavailable. Refresh to retry.');
        setStatus(value);setLoaded(true);setRoot(current=>current||value.catalog.root);
        const last=value.jobs.filter(job=>job.state==='completed').at(-1);
        if(last&&completed.current!==last.id){completed.current=last.id;void onChanged.current().catch(e=>{if(live)setError(errorText(e));});}
        if(value.jobs.some(job=>['queued','running'].includes(job.state)))timer=setTimeout(()=>void refresh(),1500);
      } catch(e){if(live)setError(errorText(e));}
    }
    void refresh();
    return()=>{live=false;clearTimeout(timer);};
  },[busy,refreshVersion]);
  const active=status?.jobs.some(job=>['queued','running'].includes(job.state));
  async function act(action:string,data:Record<string,unknown>={}){
    setBusy(action);setError('');
    try{await api(action,data);if(action==='native_configure')await changed();}
    catch(e){setError(errorText(e));}
    finally{setBusy('');}
  }
  async function scan(){
    setBusy('scan');setError('');
    try{const result=await api<{models:OllamaModel[]}>('native_inventory',{root:source});setInventory(result.models);}
    catch(e){setError(errorText(e));}finally{setBusy('');}
  }
  async function browse(kind:'root'|'source'|'files'){
    setError('');
    try{
      if(kind==='files'){const result=await native<{paths?:string[]}>('choose_model_files');if(result.paths)setPath(result.paths.join('\n'));}
      else{const result=await native<{path?:string}>('choose_model_directory');if(result.path)(kind==='root'?setRoot:setSource)(result.path);}
    }catch(e){setError(errorText(e));}
  }
  return <section className="settings-card native-models" aria-labelledby="native-models-title">
    <div className="native-models-heading"><Cpu size={20} aria-hidden="true"/><div><h3 id="native-models-title">Forge models</h3><p>Local inference, built into your workspace.</p></div><Badge>{status?.state==='generating'?'Active':status?.bundled?'Bundled':'Development build'}</Badge><IconButton label="Refresh Forge models" onClick={()=>{setError('');setRefreshVersion(v=>v+1);}}><RefreshCw size={14}/></IconButton></div>
    {!status?.bundled&&loaded&&<p className="card-note">This source build needs the pinned native runtime bundle. Packaged Forge includes it.</p>}
    <Field label="Model directory" hint="GGUF weights stay here when Forge updates. Changing this directory keeps your chats and settings."><div className="inline-form"><input aria-label="Forge model directory" value={root} onChange={e=>setRoot(e.target.value)} disabled={Boolean(active||busy)}/>{window.pywebview?.api.choose_model_directory&&<IconButton label="Browse Forge model directory" disabled={Boolean(active||busy)} onClick={()=>void browse('root')}><Folder size={14}/></IconButton>}<button className="secondary settings-action" disabled={!root||Boolean(active||busy)} onClick={()=>void act('native_configure',{root})}>Save</button></div></Field>
    {status?.catalog.models.map(model=><div className="native-model-row" key={model.id}><div><strong>{model.name}</strong><small>{model.engine==='strata'?'Strata':'llama.cpp'} · {bytesLabel(model.files.reduce((sum,file)=>sum+file.size,0))} · {model.validation==='passed'?'Text & tools checked':'Validation pending'}</small></div><IconButton label={`Check ${model.name}`} disabled={Boolean(busy||active)} onClick={()=>void act('native_validate',{id:model.id})}><Check size={14}/></IconButton><button className="secondary settings-action" aria-pressed={selected===model.id} disabled={Boolean(busy||active)} onClick={()=>void useModel(model.id).catch(e=>setError(errorText(e)))}>{selected===model.id?'Selected':'Use'}{selected===model.id?<Check size={14} aria-hidden="true"/>:<ArrowRight size={14} aria-hidden="true"/>}</button></div>)}
    <details open={advanced} onToggle={e=>setAdvanced(e.currentTarget.open)}><summary>Import or download a model</summary><div className="native-model-options">
      <Field label="GGUF file paths" hint="One path per line. Include every shard of a split model."><textarea aria-label="GGUF file paths" rows={2} value={path} onChange={e=>setPath(e.target.value)} placeholder="Full path to a .gguf file"/></Field>{window.pywebview?.api.choose_model_files&&<button className="secondary settings-action" disabled={Boolean(active||busy)} onClick={()=>void browse('files')}><Folder size={14} aria-hidden="true"/>Choose GGUF files</button>}<button className="secondary settings-action" disabled={!path.trim()||Boolean(busy||active)} onClick={()=>void act('native_import',{paths:path.split(/\r?\n/).map(p=>p.trim()).filter(Boolean)})}>Import GGUF</button>
      <Field label="Ollama model directory" hint="Choose the directory containing blobs and manifests. Imports retain the Ollama originals."><div className="inline-form"><input aria-label="Ollama model directory" value={source} onChange={e=>setSource(e.target.value)} placeholder="Path to Ollama models"/><button className="secondary settings-action" disabled={!source||Boolean(busy||active)} onClick={()=>void scan()}><RefreshCw size={14} aria-hidden="true"/>Find models</button></div></Field>
      {inventory.map(model=><div className="native-model-row" key={model.id}><div><strong>{model.name}</strong><small>{bytesLabel(model.size)}{model.has_adapter?' · Adapter requires review':''}</small></div><IconButton label={`Import ${model.name}`} disabled={model.has_adapter||Boolean(busy||active)} onClick={()=>void act('native_import_ollama',{root:source,id:model.id})}><Download size={15}/></IconButton></div>)}
      <div className="native-model-row"><div><strong>Qwen3.8 Flash Next Coder</strong><small>Strata · 58.4 GB · 32 GB RAM and 12 GB NVIDIA GPU minimum</small></div><button className="secondary settings-action" disabled={Boolean(busy||active)} onClick={()=>void act('native_download',{id:'strata-coder-iq1-m'})}><Download size={14} aria-hidden="true"/>Download</button></div>
      <p className="card-note">Strata supports this Qwen family. Other GGUF models use llama.cpp. Speed depends on the model, context and available memory.</p>
    </div></details>
    {status?.jobs.slice(-1).map(job=><div className="native-model-job" role="status" key={job.id}>{['queued','running'].includes(job.state)?<LoaderCircle size={15} className="spin" aria-hidden="true"/>:job.state==='completed'?<Check size={15} aria-hidden="true"/>:null}<span>{job.message}{job.expected_bytes&&job.received_bytes!==undefined&&<small>{bytesLabel(job.received_bytes)} of {bytesLabel(job.expected_bytes)}</small>}</span>{['queued','running'].includes(job.state)&&job.action==='native_download'&&<button className="text-button" onClick={()=>void act('native_cancel')}>Pause</button>}</div>)}
    {error&&<ErrorNotice error={error}/>}
  </section>;
}
