import type { DraftCommandMode } from './persistentDraft';
export const composerModes={goal:{label:'Goal',description:'Work through a tracked goal'},plan:{label:'Plan',description:'Inspect and prepare a plan'},do:{label:'Do',description:'Run a task with tools'},builder:{label:'Builder',description:'Explore ideas and shape a project'},todo:{label:'Checklist',description:'Run the saved plan as a checklist'}} as const;
export function isComposerMode(value:unknown):value is DraftCommandMode{return typeof value==='string' && Object.hasOwn(composerModes,value);}
export function consumeWorkflowPrefix(text:string):{mode:DraftCommandMode;text:string}|null{
  const match=/^\/(goal|plan|do|builder|todo)[\t ]+([\s\S]*)$/i.exec(text);
  if(!match)return null;return {mode:match[1].toLowerCase() as DraftCommandMode,text:match[2]};
}
export function requestMode(text:string,selected:DraftCommandMode|null):DraftCommandMode|null{
  if(!text.startsWith('/'))return selected;
  const command=/^\/([^\s]+)(?:\s|$)/i.exec(text)?.[1].toLowerCase();
  if(isComposerMode(command))return command;if(command==='to-do')return 'todo';
  return command && ['build','pause','resume','status','compact','new','project','model','agents','worktree','skill','mcp','schedule','help'].includes(command)?null:selected;
}
