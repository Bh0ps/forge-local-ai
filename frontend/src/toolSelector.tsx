import { useEffect, useState } from 'react';
import { api, errorText } from './api';
interface Tool {function:{name:string;description?:string};capability?:string;}
export function ToolSelector({value,onChange,projectId}:{value:string[];onChange:(value:string[])=>void;projectId?:string|null}){
  const [tools,setTools]=useState<Tool[]>([]);const [query,setQuery]=useState('');const [error,setError]=useState('');
  useEffect(()=>{let alive=true;void api<{tools:Tool[]}>('tools',{project_id:projectId || undefined}).then(result=>{if(alive)setTools(result.tools || []);}).catch(e=>{if(alive)setError(errorText(e));});return()=>{alive=false;};},[projectId]);
  const names=new Set(tools.map(tool=>tool.function.name));
  return <div className="skill-selector"><label>Allowed tools<input aria-label="Search profile tools" placeholder="Search tools…" value={query} onChange={e=>setQuery(e.target.value)}/></label><small>Empty selection inherits the enabled project tool set.</small>{error && <p role="alert">{error}</p>}<div className="skill-selection-chips">{value.map(name=><button type="button" key={name} onClick={()=>onChange(value.filter(item=>item!==name))}>{name}{!names.has(name)?' · unavailable':''} ×</button>)}</div><div className="skill-selector-results">{tools.filter(tool=>`${tool.function.name} ${tool.function.description || ''}`.toLowerCase().includes(query.toLowerCase())).slice(0,30).map(tool=><label key={tool.function.name}><input type="checkbox" checked={value.includes(tool.function.name)} onChange={e=>onChange(e.target.checked?[...value,tool.function.name]:value.filter(name=>name!==tool.function.name))}/>{tool.function.name}<small>{tool.capability}</small></label>)}</div></div>;
}
