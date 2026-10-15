import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { NativeModels } from '../nativeModels';

const status={state:'stopped',bundled:true,catalog:{root:'D:\\Forge\\Models',models:[]},jobs:[]};
afterEach(()=>{cleanup();delete window.pywebview;});
function bridge(handler?:(action:string,data:Record<string,unknown>)=>unknown){
  const call=vi.fn(async(action:string,data:Record<string,unknown>={})=>handler?handler(action,data):status);
  window.pywebview={api:{call}};return call;
}
describe('Forge model management',()=>{
  it('shows the owned directory without starting inference or downloads',async()=>{
    const call=bridge();render(<NativeModels changed={vi.fn(async()=>{})} useModel={vi.fn(async()=>{})}/>);
    await screen.findByDisplayValue('D:\\Forge\\Models');
    expect(call.mock.calls.every(([action])=>action==='native_status')).toBe(true);
    expect(screen.getByText('Bundled')).toBeTruthy();
  });
  it('can retry a failed status read',async()=>{
    const call=bridge();call.mockRejectedValueOnce(new Error('Synthetic offline state'));
    render(<NativeModels changed={vi.fn(async()=>{})} useModel={vi.fn(async()=>{})}/>);
    await screen.findByText('Synthetic offline state');
    fireEvent.click(screen.getByRole('button',{name:'Refresh Forge models'}));
    await screen.findByDisplayValue('D:\\Forge\\Models');
    expect(screen.queryByText('Synthetic offline state')).toBeNull();
  });
  it('imports the selected Ollama model and retains an explicit directory',async()=>{
    const call=bridge(action=>action==='native_inventory'?{models:[{id:'synthetic',name:'Synthetic model',size:1024,has_adapter:false}]}:status);
    render(<NativeModels changed={vi.fn(async()=>{})} useModel={vi.fn(async()=>{})}/>);
    await screen.findByDisplayValue('D:\\Forge\\Models');
    fireEvent.change(screen.getByLabelText('Ollama model directory'),{target:{value:'D:\\Ollama\\Models'}});
    fireEvent.click(screen.getByRole('button',{name:'Find models'}));
    fireEvent.click(await screen.findByRole('button',{name:'Import Synthetic model'}));
    await waitFor(()=>expect(call).toHaveBeenCalledWith('native_import_ollama',{root:'D:\\Ollama\\Models',id:'synthetic'}));
  });
  it('refreshes the model picker once when a completed import is observed',async()=>{
    bridge(()=>({...status,jobs:[{id:'done',state:'completed',message:'Ready in Forge.'}]}));
    const changed=vi.fn(async()=>{});render(<NativeModels changed={changed} useModel={vi.fn(async()=>{})}/>);
    await waitFor(()=>expect(changed).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole('button',{name:'Refresh Forge models'}));
    await waitFor(()=>expect(screen.getByText('Ready in Forge.')).toBeTruthy());
    expect(changed).toHaveBeenCalledTimes(1);
  });
});
