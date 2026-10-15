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
  it('downloads the standard Strata variant only after an explicit click',async()=>{
    const call=bridge();render(<NativeModels changed={vi.fn(async()=>{})} useModel={vi.fn(async()=>{})}/>);
    await screen.findByDisplayValue('D:\\Forge\\Models');
    fireEvent.click(screen.getByText('Import or download a model'));
    expect((screen.getByLabelText('Strata variant') as HTMLSelectElement).value).toBe('strata-coder-iq1-m');
    expect(call.mock.calls.some(([action])=>action==='native_download')).toBe(false);
    expect(screen.queryByText(/483 KB community projection/)).toBeNull();
    fireEvent.click(screen.getByRole('button',{name:'Download'}));
    await waitFor(()=>expect(call).toHaveBeenCalledWith('native_download',{id:'strata-coder-iq1-m'}));
  });
  it('changes the optional Strata variant without downloading or selecting a model',async()=>{
    const call=bridge(),useModel=vi.fn(async()=>{});render(<NativeModels changed={vi.fn(async()=>{})} useModel={useModel}/>);
    await screen.findByDisplayValue('D:\\Forge\\Models');
    fireEvent.click(screen.getByText('Import or download a model'));
    fireEvent.change(screen.getByLabelText('Strata variant'),{target:{value:'strata-coder-uncensored-iq1-m'}});
    expect(screen.getByText(/483 KB community projection/)).toBeTruthy();
    expect(call.mock.calls.every(([action])=>action==='native_status')).toBe(true);
    expect(useModel).not.toHaveBeenCalled();
    expect(screen.getByRole('link',{name:'Qwen Community License 1.0'})).toBeTruthy();
    fireEvent.click(screen.getByRole('button',{name:'Download'}));
    await waitFor(()=>expect(call).toHaveBeenCalledWith('native_download',{id:'strata-coder-uncensored-iq1-m'}));
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
  it('shows the selected model without changing its stable identity',async()=>{
    const useModel=vi.fn(async()=>{});
    bridge(()=>({...status,catalog:{...status.catalog,models:[{id:'stable',name:'Synthetic model',engine:'llama-cpp',validation:'passed',files:[{size:1024}]}]}}));
    render(<NativeModels changed={vi.fn(async()=>{})} useModel={useModel} selected="stable"/>);
    expect((await screen.findByRole('button',{name:'Selected'})).getAttribute('aria-pressed')).toBe('true');
    expect(useModel).not.toHaveBeenCalled();
  });
  it('keeps a malformed catalog out of the rendered model list',async()=>{
    bridge(()=>({...status,catalog:{...status.catalog,models:[{id:'broken',name:'Malformed model'}]}}));
    render(<NativeModels changed={vi.fn(async()=>{})} useModel={vi.fn(async()=>{})}/>);
    await screen.findByText('Forge model metadata is unavailable. Refresh to retry.');
    expect(screen.queryByText('Malformed model')).toBeNull();
  });
});
