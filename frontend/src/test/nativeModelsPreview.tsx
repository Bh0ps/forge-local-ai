import {useState} from 'react';
import {createRoot} from 'react-dom/client';
import {SettingsPage} from '../settings';
import {DEFAULT_SETTINGS} from '../types';
import type {Settings} from '../types';
import '../styles.css';import '../upgrade.css';import '../forge42.css';import '../workspacePolish.css';
const model={name:'qwen3-0.6b',display_name:'Qwen3 0.6B',size:639446688,capabilities:['completion','tools','thinking']};
window.pywebview={api:{call:async(action)=>{
 if(action==='native_status')return {state:'stopped',bundled:true,catalog:{root:'D:\\Forge\\Models',models:[{id:model.name,name:model.display_name,engine:'llama-cpp',validation:'passed',files:[{size:model.size}]}]},jobs:[]};
 if(action==='providers')return {providers:[{id:'forge-native',name:'Forge',kind:'forge-native',url:'forge://native'},{id:'ollama',name:'Ollama',kind:'ollama',url:'http://127.0.0.1:11434/api'}]};
 if(action==='models')return {models:[model]};
 return {};
}}};
function Preview(){const [settings,setSettings]=useState<Settings>({...DEFAULT_SETTINGS,provider_id:'forge-native',model:model.name});const [tab,setTab]=useState('models');return <main style={{height:'100dvh',overflow:'auto',padding:'30px clamp(16px,4vw,54px)'}}><p style={{color:'var(--muted)',fontSize:12}}>Forge 5.2 · Synthetic workspace</p><SettingsPage settings={settings} onChange={async delta=>setSettings(s=>({...s,...delta}))} models={[model]} refreshModels={async()=>{}} tab={tab} setTab={setTab} capabilities={{}} notify={()=>{}} applyRecommendation={async()=>{}}/></main>}
createRoot(document.getElementById('root')!).render(<Preview/>);
