import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import App, { applyEvents } from '../App';
import { DEFAULT_SETTINGS } from '../types';
import { PerformanceSettings, memoryLabel } from '../performanceSettings';
import { HuggingFaceModels } from '../huggingFaceModels';
import { ChatRow } from '../chatActions';
import { ProjectActions } from '../projectActions';
import { BrowserSettings } from '../browserSettings';
import { DictationSettings } from '../dictationSettings';

function bridge(overrides: Record<string, unknown> = {}) {
  const responses: Record<string, unknown> = { bootstrap: { settings: { ...DEFAULT_SETTINGS, model: 'fixture:9b' }, projects: [], chats: [], runs: [], capabilities: {} }, models: { models: [{ name: 'fixture:9b', size: 5e9 }] }, projects: { projects: [] }, chats: { chats: [] }, runs: { runs: [] }, providers: { providers: [{ id: 'ollama', name: 'Ollama', kind: 'ollama' }] }, settings: (data: Record<string, unknown>) => ({ ...DEFAULT_SETTINGS, model: 'fixture:9b', ...data }), show: { capabilities: ['tools','vision'], model_info: { 'fixture.context_length': 131072 } }, runtime_status: { runtimes: [] }, ...overrides };
  const call = vi.fn(async (action: string, data: Record<string, unknown> = {}) => { if (!(action in responses)) throw new Error(`Unexpected action: ${action}`); const result = responses[action]; return typeof result === 'function' ? result(data) : result; });
  window.pywebview = { api: { call, mode: vi.fn(async () => ({ ok: true })) } }; return { call, responses };
}
async function boot() { await act(async () => { window.dispatchEvent(new Event('pywebviewready')); }); }
const chat = { id: 'chat-1', project_id: null, title: 'Fixture chat', model: 'fixture:9b' };
const project = { id: 'project-1', name: 'Fixture project', path: '/synthetic/project' };
const performance = { telemetry: { cpu: { name: 'Fixture CPU', physical_cores: 8, logical_cores: 16, utilization_percent: 25 }, ram: { total_bytes: 64*1073741824, used_bytes: 16*1073741824, available_bytes: 48*1073741824, percent_used: 25 }, gpus: [{ name: 'Fixture GPU', total_bytes: 12*1073741824, used_bytes: 4*1073741824, free_bytes: 8*1073741824, utilization_percent: 15 }] }, engine: { online: true, provider_id: 'ollama', residency_supported: true, resident_models: [{ name: 'other:3b', size_bytes: 3*1073741824, size_vram_bytes: 2*1073741824 }] }, recommendations: [{ id: 'speed', title: 'Fast local workspace', description: 'Fixture model at 16K.', settings: { model: 'fixture:9b', context: 16384, performance: 'speed', thinking: false }, reason: 'Benchmark the exact model.', requires_acceptance: true }], benchmarks: [], capabilities: { benchmark: true, warm_model: true, release_model: true } };
function renderPerformance() { const change = vi.fn(async () => {}), apply = vi.fn(async () => {}), refresh = vi.fn(async () => {}); render(<PerformanceSettings settings={{ ...DEFAULT_SETTINGS, model: 'fixture:9b', provider_id: 'ollama' }} models={[{name:'fixture:9b',size:5e9}]} onChange={change} onApply={apply} notify={vi.fn()} refreshModels={refresh} />); return { change, apply, refresh }; }

describe('measured performance controls', () => {
  it('labels binary and decimal memory and distinguishes other resident models from the selection', async () => {
    expect(memoryLabel(64*1073741824)).toBe('64.0 GiB'); expect(memoryLabel(64*1073741824,'GB')).toBe('68.7 GB'); expect(memoryLabel(null)).toBe('—');
    bridge({ performance }); renderPerformance(); await screen.findByText('48.0 GiB'); expect(screen.getByText('64.0 GiB')).toBeTruthy(); expect(screen.getByText('Fixture CPU')).toBeTruthy(); expect(screen.getByText('Not loaded')).toBeTruthy();
    await userEvent.click(screen.getByText('Loaded models in this engine (1)')); expect(screen.getAllByText('other:3b').length).toBeGreaterThan(0); expect(screen.getByText('2.0 GiB VRAM')).toBeTruthy();
  });
  it('reviews a real profile recommendation before applying it and never saves a label alone', async () => {
    bridge({ performance }); const { change, apply } = renderPerformance(); await screen.findByRole('combobox', {name:'Performance profile'}); await waitFor(() => expect((screen.getByRole('combobox', {name:'Performance profile'}) as HTMLSelectElement).disabled).toBe(false));
    await userEvent.selectOptions(screen.getByRole('combobox', {name:'Performance profile'}),'speed'); expect(screen.getByRole('dialog')).toBeTruthy(); expect(apply).not.toHaveBeenCalled(); expect(change).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole('button',{name:'Apply changes'})); await waitFor(() => expect(apply).toHaveBeenCalledWith('speed')); expect(change).not.toHaveBeenCalled();
  });
  it('benchmarks selected settings and labels job-wide memory peaks without inventing per-probe peaks', async () => {
    const job = { id:'benchmark-1',status:'completed',model:'fixture:9b',provider_id:'ollama',context:32768,peak_gpu_bytes:4*1073741824,peak_ram_bytes:16*1073741824,results:[{case:'coding',status:'passed',ttft_seconds:.5,total_seconds:2,input_tokens:200,output_tokens:40,tps:20,estimated:false}] };
    const {call}=bridge({ performance, performance_benchmark:job, performance_benchmarks:{benchmarks:[job]} }); renderPerformance(); await screen.findByText('Fixture GPU'); await userEvent.click(screen.getByRole('button',{name:'Run benchmark'}));
    await waitFor(() => expect(call).toHaveBeenCalledWith('performance_benchmark',{model:'fixture:9b',provider_id:'ollama',context:32768,vision:true,tools:true})); expect(await screen.findByText('20.0 tok/s')).toBeTruthy(); expect(screen.getByText('Measured across the complete benchmark')).toBeTruthy(); expect(screen.queryByRole('columnheader',{name:'Peak VRAM'})).toBeNull();
  });
});

describe('conversation and project organization', () => {
  it('preserves left-click selection and supports right-click move with stable identity', async () => {
    const {call}=bridge({chat_move:{ok:true}}); const select=vi.fn(),changed=vi.fn(async()=>{}); render(<ChatRow chat={chat} projects={[project]} selected active={false} onSelect={select} onChanged={changed} recent />);
    await userEvent.click(screen.getByRole('button',{name:'Fixture chat'})); expect(select).toHaveBeenCalledWith('chat-1'); expect(screen.queryByRole('menu')).toBeNull();
    fireEvent.contextMenu(screen.getByRole('button',{name:'Fixture chat'}),{clientX:20,clientY:40}); await userEvent.click(screen.getByRole('menuitem',{name:'Add to project'})); await userEvent.selectOptions(screen.getByLabelText('Project'),'project-1'); await userEvent.click(screen.getByRole('button',{name:'Save'}));
    await waitFor(()=>expect(call).toHaveBeenCalledWith('chat_move',{id:'chat-1',project_id:'project-1'})); expect(changed).toHaveBeenCalledWith('chat-1','move');
  });
  it('archives and restores through explicit dialogs', async () => {
    const {call}=bridge({chat_archive:{ok:true}}); const changed=vi.fn(async()=>{}); const view=render(<ChatRow chat={chat} projects={[]} selected={false} active={false} onSelect={vi.fn()} onChanged={changed} />);
    await userEvent.click(screen.getByRole('button',{name:'Chat actions for Fixture chat'})); await userEvent.click(screen.getByRole('menuitem',{name:'Archive chat'})); expect(call.mock.calls.some(([action])=>action==='chat_archive')).toBe(false); await userEvent.click(screen.getByRole('button',{name:'Archive chat'})); await waitFor(()=>expect(changed).toHaveBeenCalledWith('chat-1','archive')); expect(call).toHaveBeenCalledWith('chat_archive',{id:'chat-1',archived:true});
    view.rerender(<ChatRow chat={{...chat,archived:true}} projects={[]} selected={false} active={false} onSelect={vi.fn()} onChanged={changed} />); await userEvent.click(screen.getByRole('button',{name:'Chat actions for Fixture chat'})); await userEvent.click(screen.getByRole('menuitem',{name:'Restore chat'})); await userEvent.click(screen.getByRole('button',{name:'Restore chat'})); await waitFor(()=>expect(call).toHaveBeenCalledWith('chat_archive',{id:'chat-1',archived:false}));
  });
  it('surfaces guarded deletion failures and never reports a deleted conversation', async () => {
    bridge({chat_delete:{error:'Inspect interrupted actions before deleting.'}}); const changed=vi.fn(async()=>{}); render(<ChatRow chat={chat} projects={[]} selected={false} active={false} onSelect={vi.fn()} onChanged={changed} />);
    await userEvent.click(screen.getByRole('button',{name:'Chat actions for Fixture chat'})); await userEvent.click(screen.getByRole('menuitem',{name:'Delete chat'})); expect(screen.getByText(/Recorded usage totals are retained/)).toBeTruthy(); await userEvent.click(screen.getByRole('button',{name:'Delete permanently'})); expect(await screen.findByRole('alert')).toHaveProperty('textContent','Inspect interrupted actions before deleting.'); expect(changed).not.toHaveBeenCalled();
  });
  it('removes only project registration after a right-click confirmation', async () => {
    const {call}=bridge({project_delete:{ok:true}}); const removed=vi.fn(async()=>{}); render(<ProjectActions project={project} onRemoved={removed}><button>Fixture project</button></ProjectActions>);
    fireEvent.contextMenu(screen.getByRole('button',{name:'Fixture project'}),{clientX:30,clientY:50}); await userEvent.click(screen.getByRole('menuitem',{name:'Delete project'})); expect(screen.getByText(/project folder and its files will stay on disk/)).toBeTruthy(); expect(call.mock.calls.some(([action])=>action==='project_delete')).toBe(false); await userEvent.click(screen.getByRole('button',{name:'Remove project'})); await waitFor(()=>expect(call).toHaveBeenCalledWith('project_delete',{id:'project-1'})); expect(removed).toHaveBeenCalledWith('project-1');
  });
});

it('does not let a delayed conversation reload replace a newer empty chat or its draft', async () => {
  let resolve!: (value:unknown)=>void; const second={...chat,id:'chat-2',title:'Second conversation'};
  bridge({bootstrap:{settings:{...DEFAULT_SETTINGS,model:'fixture:9b'},projects:[],chats:[chat,second],runs:[],capabilities:{}},chats:{chats:[chat,second]},get_chat:(data:Record<string,unknown>)=>data.id===chat.id?{...chat,messages:[]}:new Promise(done=>{resolve=done;})});render(<App/>);await boot();await screen.findByRole('button',{name:'Second conversation'});
  await userEvent.click(screen.getByRole('button',{name:'Second conversation'}));await userEvent.click(screen.getByRole('button',{name:/New chat/}));await userEvent.type(screen.getByRole('textbox',{name:'Message Forge'}),'Preserve this new draft');
  await act(async()=>resolve({...second,messages:[{role:'assistant',content:'Delayed old conversation.'}]}));expect((screen.getByRole('textbox',{name:'Message Forge'}) as HTMLTextAreaElement).value).toBe('Preserve this new draft');expect(screen.queryByText('Delayed old conversation.')).toBeNull();expect(screen.getByText('What are we building?')).toBeTruthy();
});

describe('saved plan and context workflow', () => {
  it('builds the saved completed plan by run ID instead of creating another free-form goal', async () => {
    const saved={...chat, messages:[{role:'assistant',content:'1. Inspect the project.\n2. Implement and verify.'}]}; const planRun={id:'plan-1',chat_id:chat.id,mode:'plan',status:'completed',request:'Implement fixture',created_at:'2026-01-01T00:00:00Z'};
    const {call}=bridge({bootstrap:{settings:{...DEFAULT_SETTINGS,model:'fixture:9b'},projects:[],chats:[chat],runs:[planRun],capabilities:{}},get_chat:saved,chats:{chats:[chat]},plans:{plans:[{id:'saved-plan',run_id:'plan-1',chat_id:chat.id,request:'Implement fixture',status:'ready',markdown:'1. Inspect\n2. Implement',tasks:[{text:'Inspect',status:'pending'},{text:'Implement',status:'pending'}]}]},plan_build:{id:'goal-1',chat_id:chat.id,mode:'goal',status:'queued'},poll:{events:[],finished:false,status:'running'}});
    render(<App/>); await boot(); await userEvent.click(await screen.findByRole('button',{name:'Build'})); await waitFor(()=>expect(call).toHaveBeenCalledWith('plan_build',expect.objectContaining({run_id:'plan-1',client_submission_id:expect.any(String)}))); expect(call.mock.calls.some(([action])=>action==='command')).toBe(false); expect(screen.queryByRole('button',{name:'Build'})).toBeNull();
  });
  it('exposes named context presets and warns when Ultra exceeds model capability', async () => {
    const {call}=bridge(); render(<App/>); await boot(); await screen.findByText('What are we building?'); await userEvent.click(screen.getByRole('button',{name:'Settings'})); await userEvent.click(screen.getByRole('button',{name:'Ultra 256K'})); await waitFor(()=>expect(call).toHaveBeenCalledWith('settings',{context:262144})); expect(await screen.findByText(/exceeds the model’s reported 128K limit/)).toBeTruthy(); await userEvent.click(screen.getByRole('button',{name:'Low 2K'})); await waitFor(()=>expect(call).toHaveBeenCalledWith('settings',{context:2048}));
  });
});

describe('local browser and dictation feedback', () => {
  it('opens the native browser without downloading Chromium', async () => {
    const {call}=bridge({browser_status:{native:{available:true,running:false,engine:'WebView2',capabilities:['navigate','inspect','click']},default_backend:'native'},browser_native_open:{ok:true}}); render(<BrowserSettings notify={vi.fn()}/>); await userEvent.type(screen.getByRole('textbox',{name:'Browser address'}),'https://example.com'); await userEvent.click(await screen.findByRole('button',{name:'Open browser'})); await waitFor(()=>expect(call).toHaveBeenCalledWith('browser_native_open',{url:'https://example.com'})); expect(call.mock.calls.some(([action])=>action==='browser_install')).toBe(false);
  });
  it('keeps native readiness separate from optional Chromium setup and checks it only on expansion', async () => {
    const {call}=bridge({browser_status:{native:{available:true,running:false,message:'Windows browser ready.'},default_backend:'native',available:true,message:'Windows browser ready.',isolated:{available:false,dependency_available:true,message:'Optional Chromium needs installation.'}}}); render(<BrowserSettings notify={vi.fn()}/>); await screen.findByText('Windows browser ready.'); expect(call).toHaveBeenCalledWith('browser_status',{}); expect(call.mock.calls.some(([,data])=>data?.backend==='isolated')).toBe(false);
    await userEvent.click(screen.getByText('Optional isolated browser')); await waitFor(()=>expect(call).toHaveBeenCalledWith('browser_status',{backend:'isolated'})); expect(screen.getByRole('button',{name:'Install browser'})).toBeTruthy(); expect(screen.getByText('Optional Chromium needs installation.')).toBeTruthy();
  });
  it('prevents duplicate microphone starts while loading and makes native state errors visible', async () => {
    let resolve!: (value:unknown)=>void; const {call}=bridge({dictation_start:()=>new Promise(done=>{resolve=done;})}); render(<App/>); await boot(); await screen.findByText('What are we building?'); await userEvent.click(screen.getByRole('button',{name:'Start dictation'})); expect((screen.getByRole('button',{name:'Preparing dictation'}) as HTMLButtonElement).disabled).toBe(true); expect(screen.getByText('Loading local dictation…')).toBeTruthy(); await act(async()=>resolve({state:'error',message:'The microphone is unavailable.',setup_required:true})); expect(await screen.findByText('The microphone is unavailable.')).toBeTruthy(); expect(screen.getByRole('button',{name:'Open Dictation settings'})).toBeTruthy(); expect(call.mock.calls.filter(([action])=>action==='dictation_start')).toHaveLength(1); expect(call.mock.calls.some(([action])=>action==='dictation_status')).toBe(false);
  });
  it('always labels context and generation speed in both modes and keeps the HUD reply toggle in its header', async () => {
    bridge(); render(<App/>); await boot(); await screen.findByText('What are we building?'); expect(screen.getByText('32K context')).toBeTruthy(); expect(screen.getByLabelText('Generation speed in tokens per second').textContent).toBe('Tokens/s: —');
    await userEvent.click(screen.getByRole('button',{name:'Minimal HUD'})); expect(screen.getByText('32K context')).toBeTruthy(); expect(screen.getByLabelText('Generation speed in tokens per second').textContent).toBe('Tokens/s: —'); expect(screen.getByRole('button',{name:'Show reply'}).closest('header')).toBeTruthy(); expect(document.querySelectorAll('.composer-status i')).toHaveLength(1);
  });
  it('reports an empty transcript without claiming that text was inserted', async () => {
    bridge({dictation_start:{state:'done',text:''}}); render(<App/>); await boot(); await screen.findByText('What are we building?'); await userEvent.click(screen.getByRole('button',{name:'Start dictation'})); expect(await screen.findByText(/No speech detected/)).toBeTruthy(); expect(screen.queryByText('Transcript added to your draft.')).toBeNull();
  });
  it('recognizes an installed idle dictation model as ready', async () => {
    bridge({dictation_status:{state:'idle',model:'base',model_installed:true,setup_required:false,device:'cpu',compute_type:'int8'}}); render(<DictationSettings settings={{...DEFAULT_SETTINGS,dictation_model:'base'}} onChange={vi.fn(async()=>{})} notify={vi.fn()}/>); expect(await screen.findByText('Ready')).toBeTruthy(); expect(screen.getByRole('button',{name:'Verify installed model'})).toBeTruthy();
  });
});

describe('reviewed Hugging Face model installation', () => {
  const revision='a'.repeat(40); const files=[{path:'model-Q4_K_M.gguf',size:2e9,quantization:'Q4_K_M',is_projector:false,downloadable:true},{path:'mmproj-f16.gguf',size:1e8,quantization:'F16',is_projector:true,downloadable:true}];
  const download={id:'download-1',kind:'download',status:'completed',repo_id:'example/model-GGUF',revision,license:'MIT',vision:true,files,total_bytes:2.1e9,completed_bytes:2.1e9};
  it('downloads only selected reviewed files from the immutable repository revision', async () => {
    const {call}=bridge({hf_status:{dependency_available:true,authenticated:false},hf_jobs:{jobs:[]},hf_search:{models:[{repo_id:'example/model-GGUF',license:'MIT'}]},hf_files:{repo_id:'example/model-GGUF',revision,license:'MIT',vision:true,files},hf_download:{job:{...download,status:'queued'}}}); render(<HuggingFaceModels notify={vi.fn()} refreshModels={vi.fn(async()=>{})}/>); await userEvent.type(screen.getByRole('textbox',{name:'Search Hugging Face models'}),'fixture'); await userEvent.click(screen.getByRole('button',{name:'Search'})); await userEvent.click(await screen.findByRole('button',{name:'Choose files'})); await userEvent.click(screen.getByRole('checkbox',{name:/model-Q4_K_M/})); await userEvent.click(screen.getByRole('checkbox',{name:/mmproj-f16/})); expect(screen.getAllByText('MIT').length).toBeGreaterThan(0); await userEvent.click(screen.getByRole('button',{name:'Download selected files'})); await waitFor(()=>expect(call).toHaveBeenCalledWith('hf_download',{repo_id:'example/model-GGUF',revision,filenames:['model-Q4_K_M.gguf','mmproj-f16.gguf']}));
  });
  it('requires a vision projector or explicit text-only import and refreshes models after completion', async () => {
    const imported={...download,id:'import-1',kind:'import',model_name:'fixture-vision:q4_k_m',capabilities:['vision','tools']}; const {call,responses}=bridge({hf_status:{dependency_available:true},hf_jobs:{jobs:[download]},hf_import:()=>{responses.hf_jobs={jobs:[imported,download]};return{job:{...imported,status:'queued'}};}}); const refresh=vi.fn(async()=>{}); render(<HuggingFaceModels notify={vi.fn()} refreshModels={refresh}/>); await userEvent.click(await screen.findByRole('button',{name:'Import into Ollama'})); expect((screen.getByRole('button',{name:'Import model'}) as HTMLButtonElement).disabled).toBe(true); await userEvent.selectOptions(screen.getByLabelText('Vision projector'),'mmproj-f16.gguf'); await userEvent.clear(screen.getByLabelText('Ollama model name')); await userEvent.type(screen.getByLabelText('Ollama model name'),'fixture-vision:q4_k_m'); await userEvent.click(screen.getByRole('button',{name:'Import model'})); await waitFor(()=>expect(call).toHaveBeenCalledWith('hf_import',{job_id:'download-1',filename:'model-Q4_K_M.gguf',model_name:'fixture-vision:q4_k_m',provider_id:'ollama',projector:'mmproj-f16.gguf',text_only:false})); await waitFor(()=>expect(refresh).toHaveBeenCalledOnce());
  });
  it('requires inspection of an unknown import outcome instead of automatically retrying it', async () => {
    const {call}=bridge({hf_status:{dependency_available:true},hf_jobs:{jobs:[{...download,kind:'import',status:'import_unknown'}]},hf_reconcile:{ok:true}}); render(<HuggingFaceModels notify={vi.fn()} refreshModels={vi.fn(async()=>{})}/>); expect(await screen.findByRole('button',{name:'Check import outcome'})).toBeTruthy(); expect(screen.queryByRole('button',{name:'Retry'})).toBeNull(); expect(call.mock.calls.some(([action])=>action==='hf_retry')).toBe(false); await userEvent.click(screen.getByRole('button',{name:'Check import outcome'})); await waitFor(()=>expect(call).toHaveBeenCalledWith('hf_reconcile',{id:'download-1'}));
  });
});

it('replaces estimated generation speed with backend timings and resets it between rounds', () => {
  const initial={run:{id:'run-1'},text:'',thinking:'',parts:[],tools:[],cursor:0,status:'running',finished:false};
  const estimated=applyEvents(initial,[{seq:1,type:'token',text:'Fixture'},{seq:2,type:'generation',tps:12,estimated:true}],{}); expect(estimated.speedEstimated).toBe(true);
  const exact=applyEvents(estimated,[{seq:3,type:'generation',tps:20,estimated:false}],{}); expect(exact.speed).toBe(20); expect(exact.speedEstimated).toBe(false);
  const next=applyEvents(exact,[{seq:4,type:'round'}],{}); expect(next.speed).toBeUndefined(); expect(next.firstToken).toBeUndefined();
});
