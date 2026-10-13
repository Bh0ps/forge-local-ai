import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { GitHubSettings } from '../githubSettings';
import { OpenRouterSettings } from '../openRouterSettings';
import { updateReadiness } from '../updateReadiness';
const attempt='a'.repeat(32);
afterEach(()=>{updateReadiness.aborted(attempt);delete window.pywebview;});
describe('connection draft ownership',()=>{
  it('retains a newer GitHub credential after an older save response and across settings navigation',async()=>{
    let complete!:(value:unknown)=>void;const call=vi.fn(async(action:string)=>action==='github_connect'?new Promise(resolve=>{complete=resolve;}):action==='github_status'?{connected:false}:action==='projects'?{projects:[]}:{});window.pywebview={api:{call}};
    const first=render(<GitHubSettings notify={()=>{}}/>);fireEvent.change(screen.getByLabelText('GitHub token'),{target:{value:'synthetic-first-token'}});fireEvent.click(screen.getByRole('button',{name:'Connect'}));await waitFor(()=>expect(complete).toBeTruthy());fireEvent.change(screen.getByLabelText('GitHub token'),{target:{value:'synthetic-newer-token'}});await act(async()=>complete({ok:true}));
    expect((screen.getByLabelText('GitHub token') as HTMLInputElement).value).toBe('synthetic-newer-token');first.unmount();const second=render(<GitHubSettings notify={()=>{}}/>);expect((screen.getByLabelText('GitHub token') as HTMLInputElement).value).toBe('synthetic-newer-token');
    updateReadiness.start(attempt,vi.fn());expect(await updateReadiness.confirm(attempt)).toEqual({ok:false,reason:'unsaved_setup'});expect(JSON.stringify(updateReadiness.getSnapshot())).not.toContain('synthetic-newer-token');updateReadiness.aborted(attempt);fireEvent.click(screen.getByRole('button',{name:'Discard connection input'}));second.unmount();
  });
  it('does not let a delayed OpenRouter status overwrite locally edited consent',async()=>{
    let complete!:(value:unknown)=>void;const call=vi.fn(async(action:string)=>action==='openrouter_status'?new Promise(resolve=>{complete=resolve;}):{});window.pywebview={api:{call}};
    const view=render(<OpenRouterSettings notify={()=>{}}/>);await waitFor(()=>expect(complete).toBeTruthy());fireEvent.click(screen.getByRole('switch',{name:'Allow assigned context to leave this computer'}));fireEvent.change(screen.getByLabelText('OpenRouter API key'),{target:{value:'synthetic-key'}});
    await act(async()=>complete({provider:{remote_consent:false,enabled:false,data_collection:'deny'}}));expect(screen.getByRole('switch',{name:'Allow assigned context to leave this computer'}).getAttribute('aria-checked')).toBe('true');expect((screen.getByLabelText('OpenRouter API key') as HTMLInputElement).value).toBe('synthetic-key');
    fireEvent.click(screen.getByRole('button',{name:'Discard connection input'}));expect((screen.getByLabelText('OpenRouter API key') as HTMLInputElement).value).toBe('');view.unmount();
  });
});
