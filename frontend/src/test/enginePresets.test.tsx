import { act,fireEvent,render,screen,waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe,expect,it,vi } from 'vitest';
import { EngineEditor } from '../engineEditor';
const provider={id:'saved',name:'My engine',type:'openai',base_url:'http://127.0.0.1:8080/v1',context_limit:8192,capabilities:['tools']};
const preset={id:'lm-studio',name:'LM Studio',kind:'openai-compatible',url:'http://127.0.0.1:1234/v1',setup_url:'https://lmstudio.ai/docs/developer/openai-compat',description:'Start the local server.',hosting:'desktop',metadata_strategy:'lmstudio'};
function bridge(test:unknown={ok:true,model_count:1}){const call=vi.fn(async(action:string)=>action==='provider_presets'?{presets:[preset]}:typeof test==='function'?test(action):test);window.pywebview={api:{call}};return call;}
describe('engine presets',()=>{
  it('keeps preset connection fields primary and the editable protocol in a closed native disclosure',async()=>{
    const call=bridge();render(<EngineEditor provider={{...provider,preset_id:'lm-studio'}} close={()=>{}} saved={async()=>{}}/>);
    await screen.findByRole('option',{name:'LM Studio'});
    const summary=screen.getByText('Advanced'),details=summary.closest('details')!;
    expect(details.open).toBe(false);
    expect(screen.getByLabelText('Name')).toBeTruthy();expect(screen.getByLabelText('Base URL')).toBeTruthy();expect(screen.getByLabelText('API key (optional)')).toBeTruthy();
    expect(screen.getByRole('link',{name:'Official setup guide'})).toBeTruthy();
    await userEvent.click(summary);await waitFor(()=>expect(details.open).toBe(true));
    expect((screen.getByLabelText('API protocol') as HTMLSelectElement).value).toBe('openai');
    expect(call.mock.calls.map(([action])=>action)).toEqual(['provider_presets']);
  });
  it('exposes custom protocol choices and keeps focus and key isolation when overriding a preset',async()=>{
    const call=bridge();render(<EngineEditor provider={provider} close={()=>{}} saved={async()=>{}}/>);
    const details=screen.getByText('Advanced').closest('details')!;expect(details.open).toBe(true);
    await screen.findByRole('option',{name:'LM Studio'});await userEvent.selectOptions(screen.getByLabelText('Engine preset'),'lm-studio');
    await waitFor(()=>expect(details.open).toBe(false));
    await userEvent.click(screen.getByText('Advanced'));await waitFor(()=>expect(details.open).toBe(true));
    await userEvent.type(screen.getByLabelText('API key (optional)'),'synthetic-key');
    const protocol=screen.getByLabelText('API protocol');await userEvent.selectOptions(protocol,'ollama');
    expect(document.activeElement).toBe(protocol);expect(details.open).toBe(true);
    expect((screen.getByLabelText('API key (optional)') as HTMLInputElement).value).toBe('');
    expect((screen.getByLabelText('Engine preset') as HTMLSelectElement).value).toBe('');
    expect((screen.getByLabelText('Base URL') as HTMLInputElement).value).toBe(preset.url);
    await userEvent.click(screen.getByRole('button',{name:'Test connection'}));
    expect(call).toHaveBeenCalledWith('provider_test',expect.objectContaining({draft:{kind:'ollama',url:preset.url,preset_id:null},api_key:undefined}));
    expect(call.mock.calls.some(([action])=>action==='provider_save'||action==='settings')).toBe(false);
  });
  it('returns custom protocol controls when leaving a preset and resets disclosure state for a replaced target',async()=>{
    bridge();const view=render(<EngineEditor provider={{...provider,preset_id:'lm-studio'}} close={()=>{}} saved={async()=>{}}/>);
    await screen.findByRole('option',{name:'LM Studio'});const details=screen.getByText('Advanced').closest('details')!;
    await userEvent.selectOptions(screen.getByLabelText('Engine preset'),'');await waitFor(()=>expect(details.open).toBe(true));
    expect((screen.getByLabelText('Base URL') as HTMLInputElement).value).toBe(provider.base_url);
    view.rerender(<EngineEditor provider={{...provider,id:'replacement',preset_id:'lm-studio'}} close={()=>{}} saved={async()=>{}}/>);
    await waitFor(()=>expect(details.open).toBe(false));expect(document.activeElement).toBe(screen.getByLabelText('Name'));
  });
  it('fills editable defaults, clears only typed credentials and saves no capability guesses',async()=>{
    const call=bridge(),saved=vi.fn(async()=>{});render(<EngineEditor provider={provider} close={()=>{}} saved={saved}/>);
    await userEvent.type(screen.getByLabelText('API key (optional)'),'synthetic-key');await screen.findByRole('option',{name:'LM Studio'});await userEvent.selectOptions(screen.getByLabelText('Engine preset'),'lm-studio');
    expect((screen.getByLabelText('Base URL') as HTMLInputElement).value).toBe(preset.url);expect((screen.getByLabelText('Name') as HTMLInputElement).value).toBe('My engine');expect((screen.getByLabelText('API key (optional)') as HTMLInputElement).value).toBe('');
    const link=screen.getByRole('link',{name:'Official setup guide'});expect(link.getAttribute('href')).toBe(preset.setup_url);expect(link.getAttribute('rel')).toContain('noreferrer');
    fireEvent.change(screen.getByLabelText('Base URL'),{target:{value:'http://127.0.0.1:9999/v1'}});await userEvent.click(screen.getByRole('button',{name:'Save engine'}));
    expect(call).toHaveBeenCalledWith('provider_save',{id:'saved',name:'My engine',kind:'openai-compatible',url:'http://127.0.0.1:9999/v1',preset_id:'lm-studio',api_key:undefined});expect(saved).toHaveBeenCalledOnce();
  });
  it('shows advertised facts separately from validation and never changes profile settings on test',async()=>{
    const call=bridge({ok:true,model_count:1,metadata:{state:'available',source:'advertised',models:[{model:'synthetic-model',max_context_length:131072,loaded_context_lengths:[8192],loaded_instances:[{model:'loaded-instance',context_length:8192}],vision:null,trained_for_tool_use:true,reasoning_options:['on','off']}]}});
    render(<EngineEditor provider={{...provider,preset_id:'lm-studio'}} close={()=>{}} saved={vi.fn(async()=>{})}/>);await userEvent.click(screen.getByRole('button',{name:'Test connection'}));
    expect(call).toHaveBeenCalledWith('provider_test',expect.objectContaining({metadata:true,draft:expect.objectContaining({preset_id:'lm-studio'})}));await userEvent.click(await screen.findByText('Advertised model support'));
    expect(screen.getByText(/Vision: Not reported/)).toBeTruthy();expect(screen.getByText(/Advertised; not validated/)).toBeTruthy();expect(screen.getByText(/loaded-instance \(8K context\)/)).toBeTruthy();expect(call.mock.calls.some(([action])=>action==='settings'||action==='provider_save')).toBe(false);
  });
  it('does not publish a delayed test from a replaced saved target or keep its key',async()=>{
    let resolve!:(value:unknown)=>void;bridge(()=>new Promise(done=>{resolve=done;}));const view=render(<EngineEditor provider={provider} close={()=>{}} saved={async()=>{}}/>);
    await userEvent.type(screen.getByLabelText('API key (optional)'),'synthetic-key');await userEvent.click(screen.getByRole('button',{name:'Test connection'}));view.rerender(<EngineEditor provider={{...provider,id:'new-target',name:'New engine'}} close={()=>{}} saved={async()=>{}}/>);
    await act(async()=>resolve({ok:true,model_count:99}));expect(screen.queryByText('Connection verified')).toBeNull();expect((screen.getByLabelText('API key (optional)') as HTMLInputElement).value).toBe('');expect((screen.getByRole('button',{name:'Test connection'}) as HTMLButtonElement).disabled).toBe(false);
  });
  it('keeps custom endpoint testing available after catalog failure and retries the read only',async()=>{
    const call=vi.fn().mockRejectedValueOnce(new Error('synthetic')).mockResolvedValue({presets:[preset]});window.pywebview={api:{call}};render(<EngineEditor provider={provider} close={()=>{}} saved={async()=>{}}/>);
    await userEvent.click(await screen.findByRole('button',{name:'Retry presets'}));await screen.findByRole('option',{name:'LM Studio'});expect(call.mock.calls.map(([action])=>action)).toEqual(['provider_presets','provider_presets']);expect((screen.getByLabelText('Base URL') as HTMLInputElement).value).toBe(provider.base_url);
  });
});
