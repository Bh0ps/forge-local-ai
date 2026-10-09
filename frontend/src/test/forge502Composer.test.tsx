import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { TaskContextPanel } from '../taskContextPanel';
import { TaskStatus } from '../taskStatus';
import { slashCommands } from '../components';

describe('5.0.2 composer', () => {
  it('searches skills and notes together and removes a selected reference', async () => {
    window.pywebview={api:{call:vi.fn(async()=>({skills:[{id:'design',name:'Interface design',description:'Colour and spacing',enabled:true},{id:'disabled',name:'Retired design',enabled:false}]}))}};
    const skills=vi.fn(), notes=vi.fn();
    render(<TaskContextPanel skills={['design']} notes={['note']} spaces={[{id:'note',name:'Brand colours'},{id:'other',name:'Other project',project_ids:['other']}]} projectId="project" onSkills={skills} onNotes={notes}/>);
    await screen.findByRole('checkbox',{name:/Interface design/});
    expect(screen.queryByText('Other project')).toBeNull();
    expect((screen.getByRole('checkbox',{name:/Retired design/}) as HTMLInputElement).disabled).toBe(true);
    fireEvent.change(screen.getByRole('textbox',{name:'Search skills and notes'}),{target:{value:'colour'}});
    expect(screen.getByRole('checkbox',{name:/Brand colours/})).toBeTruthy();
    expect(screen.queryByRole('checkbox',{name:/Retired design/})).toBeNull();
    fireEvent.click(screen.getByRole('button',{name:'Remove Brand colours'})); expect(notes).toHaveBeenCalledWith([]);
    fireEvent.click(screen.getByRole('checkbox',{name:/Interface design/})); expect(skills).toHaveBeenCalledWith([]);
  });

  it('keeps Resume icon-only and waits for inspection before enabling it', async () => {
    let finish!:(value:unknown)=>void;
    window.pywebview={api:{call:vi.fn(()=>new Promise(resolve=>{finish=resolve;}))}};
    const resume=vi.fn();
    render(<TaskStatus compact run={{id:'paused',status:'paused',recovery:'Increase context before resuming.'}} notify={()=>{}} resume={resume} pause={()=>{}}/>);
    const button=screen.getByRole('button',{name:'Resume'}) as HTMLButtonElement;
    expect(button.textContent).toBe(''); expect(button.disabled).toBe(true);
    expect(screen.queryByText('Increase context before resuming.')).toBeNull();
    await act(async()=>finish({actions:[]}));
    await waitFor(()=>expect(button.disabled).toBe(false));
    fireEvent.click(button); expect(resume).toHaveBeenCalledOnce();
    fireEvent.click(screen.getByRole('button',{name:'Task details and recovery'}));
    expect(screen.getByText('Increase context before resuming.')).toBeTruthy();
  });

  it('does not bypass unknown-action safeguards in the compact control', async () => {
    window.pywebview={api:{call:vi.fn(async()=>({actions:[{id:'a',name:'write_file',arguments:{path:'page.html'},status:'outcome_unknown'}]}))}};
    render(<TaskStatus compact run={{id:'paused',status:'paused'}} notify={()=>{}} resume={()=>{}} pause={()=>{}}/>);
    await screen.findByRole('button',{name:'Inspect write_file'});
    expect((screen.getByRole('button',{name:'Resume'}) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole('button',{name:'Inspect write_file'}));
    expect(screen.getByRole('dialog')).toBeTruthy();
  });

  it('exposes guided Builder commands in the slash picker', () => {
    expect(slashCommands.some(([name])=>name==='builder')).toBe(true);
    expect(slashCommands.some(([name])=>name==='build')).toBe(true);
  });
});
