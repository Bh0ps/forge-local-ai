import { useEffect, useState } from 'react';
import { BookOpen, Check, Code2, FlaskConical, Folder, Globe, Palette } from 'lucide-react';
import { Field } from './components';
import type { Accent, Project, ProjectAppearance, Settings, WorkspaceAppearance } from './types';
import { useRetainedScratch } from './retainedScratch';

export const ACCENTS: {id: Accent; label: string}[] = [{id:'copper',label:'Copper'}, {id:'sage',label:'Sage'}, {id:'blue',label:'Blue'}, {id:'violet',label:'Violet'}, {id:'rose',label:'Rose'}];
export const PROJECT_ICONS = {folder:Folder, code:Code2, globe:Globe, book:BookOpen, flask:FlaskConical, palette:Palette};
export const DEFAULT_APPEARANCE: WorkspaceAppearance = {name:'Forge',accent:'copper',motion:'system'};
export function appearance(settings: Settings): WorkspaceAppearance { return {...DEFAULT_APPEARANCE,...settings.ui_appearance}; }

export function ProjectIcon({project,size=15}: {project:Project;size?:number}) {
  const Icon=PROJECT_ICONS[project.appearance?.icon || 'folder'] || Folder;
  return <span className="project-identity" data-accent={project.appearance?.accent}><Icon size={size}/></span>;
}
export function AccentChoices({value,change,inherit=false,previews=false}: {value?:Accent;change:(value?:Accent)=>void;inherit?:boolean;previews?:boolean}) {
  return <div className={`accent-choices${previews?' accent-presets':''}`} role="group" aria-label="Accent colour">
    {inherit && <button type="button" aria-pressed={!value} onClick={()=>change(undefined)}>Workspace default</button>}
    {ACCENTS.map(option=><button type="button" key={option.id} data-accent={option.id} aria-label={option.label} aria-pressed={value===option.id} onClick={()=>change(option.id)}>{previews && <span className="accent-preview" role="img" aria-label={`${option.label} workspace preview`}><span className="accent-preview-sidebar"><span className="accent-preview-brand"/><span/><span className="accent-preview-selected"/><span/></span><span className="accent-preview-chat"><span className="accent-preview-user"/><span className="accent-preview-line"/><span className="accent-preview-line short"/><span className="accent-preview-composer"><span/></span></span></span>}<span className="accent-preset-label"><i aria-hidden="true"/>{option.label}{previews && value===option.id && <Check className="accent-preset-check" size={12} aria-hidden="true"/>}</span></button>)}
  </div>;
}
export function AppearanceSettings({settings,onChange}: {settings:Settings;onChange:(delta:Partial<Settings>)=>Promise<void>}) {
  const current=appearance(settings), nameDraft=useRetainedScratch('workspace-name',current.name,'unsaved_setup'),[saving,setSaving]=useState(false);
  const name=nameDraft.value,setName=nameDraft.update;
  useEffect(()=>{if(name===current.name && nameDraft.dirty)nameDraft.accepted(current.name);},[current.name,name]);
  const update=(delta:Partial<WorkspaceAppearance>)=>onChange({ui_appearance:{...current,...delta}});
  return <section className="settings-card appearance-settings"><Field label="Workspace name"><div className="workspace-name-control"><input value={name} maxLength={60} onChange={event=>setName(event.target.value)} aria-label="Workspace name"/><button className="secondary" disabled={saving || !name.trim() || name.trim()===current.name} onClick={()=>{setSaving(true);void update({name:name.trim()}).finally(()=>setSaving(false));}}>{saving?'Saving…':'Save name'}</button>{nameDraft.dirty && <button type="button" className="text-button" onClick={nameDraft.discard}>Discard name</button>}</div></Field>
    <Field label="Workspace accent"><AccentChoices previews value={current.accent} change={accent=>void update({accent:accent!})}/></Field>
    <Field label="Motion"><select value={current.motion} onChange={event=>void update({motion:event.target.value as WorkspaceAppearance['motion']})}><option value="system">Follow system preferences</option><option value="reduced">Reduced motion</option></select></Field>
  </section>;
}
export function ProjectAppearanceFields({value,change}: {value:ProjectAppearance;change:(value:ProjectAppearance)=>void}) {
  return <><Field label="Project icon"><div className="project-icon-choices" role="group" aria-label="Project icon">{Object.entries(PROJECT_ICONS).map(([key,Icon])=><button type="button" key={key} aria-label={key} aria-pressed={value.icon===key} onClick={()=>change({...value,icon:key as ProjectAppearance['icon']})}><Icon size={20}/></button>)}</div></Field><Field label="Project accent"><AccentChoices value={value.accent} inherit change={accent=>change({icon:value.icon,...(accent?{accent}:{})})}/></Field></>;
}
