import { act,fireEvent,render,screen,waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach,describe,expect,it,vi } from 'vitest';
import { McpSettings,type McpServer } from '../mcpSettings';
import { updateReadiness } from '../updateReadiness';

const fixture:McpServer={id:'synthetic-tools',name:'Synthetic tools',transport:'http',url:'https://synthetic.example/mcp',enabled:true,status:'ready',auth_type:'none',tools:[{name:'read'},{name:'write'}],enabled_tools:['read','write']};
const copy=<T,>(value:T):T=>JSON.parse(JSON.stringify(value));
const attempt='a'.repeat(32);
afterEach(()=>{updateReadiness.aborted(attempt);updateReadiness.dismiss();});
function bridge(save?:(data:Record<string,unknown>)=>unknown){
  let server=copy(fixture);const call=vi.fn(async(action:string,data:Record<string,unknown>={})=>{
    if(action==='mcp_servers')return {servers:[copy(server)]};
    if(action==='mcp_save'){if(save)return save(data);server={...server,enabled_tools:data.enabled_tools as string[]};return {ok:true,server:copy(server)};}return {};
  });window.pywebview={api:{call}};return {call,current:()=>server,stored:(next:McpServer)=>{server=copy(next);}};
}
async function open(){await userEvent.click(await screen.findByRole('button',{name:'Tools for Synthetic tools'}));await screen.findByRole('switch',{name:'write'});}

describe('authoritative MCP tool permissions',()=>{
  it('keeps a revoked permission across reopening and never sends it when another tool is deselected',async()=>{
    const {call,current}=bridge();render(<McpSettings notify={vi.fn()}/>);await open();await userEvent.click(screen.getByRole('switch',{name:'write'}));await waitFor(()=>expect(current().enabled_tools).toEqual(['read']));await userEvent.click(screen.getByRole('button',{name:'Close dialog'}));await open();
    expect(screen.getByRole('switch',{name:'write'}).getAttribute('aria-checked')).toBe('false');await userEvent.click(screen.getByRole('switch',{name:'read'}));await waitFor(()=>expect(current().enabled_tools).toEqual([]));expect(call.mock.calls.filter(([action])=>action==='mcp_save').map(([,data])=>data?.enabled_tools)).toEqual([['read'],[]]);
  });
  it('adopts the actual saved selection instead of assuming the requested selection was accepted',async()=>{
    const saved={...fixture,enabled_tools:[]};bridge(()=>({ok:true,server:copy(saved)}));render(<McpSettings notify={vi.fn()}/>);await open();await userEvent.click(screen.getByRole('switch',{name:'write'}));await waitFor(()=>expect(screen.getByRole('switch',{name:'read'}).getAttribute('aria-checked')).toBe('false'));expect(screen.getByRole('switch',{name:'write'}).getAttribute('aria-checked')).toBe('false');
  });
  it('waits for a closed view’s pending save before allowing a rapidly reopened view to edit',async()=>{
    let complete!:(value:unknown)=>void;const data=bridge(()=>new Promise(resolve=>{complete=resolve;}));render(<McpSettings notify={vi.fn()}/>);await open();await userEvent.click(screen.getByRole('switch',{name:'write'}));await waitFor(()=>expect(complete).toBeTruthy());await userEvent.click(screen.getByRole('button',{name:'Close dialog'}));await userEvent.click(screen.getByRole('button',{name:'Tools for Synthetic tools'}));
    expect(screen.queryByRole('switch',{name:'read'})).toBeNull();expect(screen.getByRole('status').textContent).toBe('Loading tool permissions…');expect(data.call.mock.calls.filter(([action])=>action==='mcp_save')).toHaveLength(1);
    const saved={...fixture,enabled_tools:['read']};data.stored(saved);await act(async()=>complete({ok:true,server:copy(saved)}));await screen.findByRole('switch',{name:'write'});expect(screen.getByRole('switch',{name:'write'}).getAttribute('aria-checked')).toBe('false');
  });
  it('keeps failed selections across close/reopen and does not re-enable an unchosen tool on the next save',async()=>{
    let failed=true;const data=bridge((payload)=>failed?{error:'Synthetic permission save failed'}:{ok:true,server:{...fixture,enabled_tools:payload.enabled_tools}});render(<McpSettings notify={vi.fn()}/>);await open();await userEvent.click(screen.getByRole('switch',{name:'write'}));await screen.findByRole('alert');expect(screen.getByRole('switch',{name:'write'}).getAttribute('aria-checked')).toBe('false');await userEvent.click(screen.getByRole('button',{name:'Close dialog'}));await open();
    expect(screen.getByRole('switch',{name:'write'}).getAttribute('aria-checked')).toBe('false');expect(screen.getByRole('alert').textContent).toContain('not been saved');failed=false;await userEvent.click(screen.getByRole('switch',{name:'read'}));await waitFor(()=>expect(screen.queryByRole('alert')).toBeNull());expect(data.call.mock.calls.filter(([action])=>action==='mcp_save').map(([,payload])=>payload?.enabled_tools)).toEqual([['read'],[]]);
  });
  it('blocks readiness for a failed selection after Settings unmounts and reconciles it from the same private owner',async()=>{
    let failed=true;bridge(payload=>failed?{error:'Synthetic permission save failed'}:{ok:true,server:{...fixture,enabled_tools:payload.enabled_tools}});const first=render(<McpSettings notify={vi.fn()}/>);await open();await userEvent.click(screen.getByRole('switch',{name:'write'}));await screen.findByRole('alert');await userEvent.click(screen.getByRole('button',{name:'Close dialog'}));first.unmount();
    updateReadiness.start(attempt,vi.fn());expect(await updateReadiness.confirm(attempt)).toEqual({ok:false,reason:'unsaved_setup'});expect(JSON.stringify(updateReadiness.getSnapshot())).not.toContain('write');updateReadiness.aborted(attempt);
    const second=render(<McpSettings notify={vi.fn()}/>);await open();expect(screen.getByRole('switch',{name:'write'}).getAttribute('aria-checked')).toBe('false');failed=false;await userEvent.click(screen.getByRole('button',{name:'Retry'}));await waitFor(()=>expect(screen.queryByRole('alert')).toBeNull());await userEvent.click(screen.getByRole('button',{name:'Close dialog'}));second.unmount();
    updateReadiness.start(attempt,vi.fn());expect((await updateReadiness.confirm(attempt)).ok).toBe(true);
  });
  it('keeps a pending save owned across Settings remount and rejects an incomplete successful response',async()=>{
    let complete!:(value:unknown)=>void;const data=bridge(()=>new Promise(resolve=>{complete=resolve;}));const first=render(<McpSettings notify={vi.fn()}/>);await open();await userEvent.click(screen.getByRole('switch',{name:'write'}));await waitFor(()=>expect(complete).toBeTruthy());first.unmount();
    updateReadiness.start(attempt,vi.fn());expect((await updateReadiness.confirm(attempt)).ok).toBe(false);updateReadiness.aborted(attempt);render(<McpSettings notify={vi.fn()}/>);await userEvent.click(await screen.findByRole('button',{name:'Tools for Synthetic tools'}));expect(screen.queryByRole('switch',{name:'read'})).toBeNull();
    await act(async()=>complete({ok:true,server:{...fixture,enabled_tools:undefined}}));await screen.findByRole('switch',{name:'write'});expect(screen.getByRole('switch',{name:'write'}).getAttribute('aria-checked')).toBe('false');expect(screen.getByRole('alert').textContent).toContain('not been saved');expect(data.call.mock.calls.filter(([action])=>action==='mcp_save')).toHaveLength(1);
    await userEvent.click(screen.getByRole('button',{name:'Close dialog'}));await userEvent.click(screen.getByRole('button',{name:'Discard unsaved tool selections'}));
  });
  it('ignores an earlier view’s delayed load when another connection opens',async()=>{
    let finish!:(value:unknown)=>void,reads=0;const other={...fixture,id:'other-tools',name:'Other tools',tools:[{name:'lookup'}],enabled_tools:['lookup']};const call=vi.fn(async(action:string)=>action==='mcp_servers'?(++reads===2?new Promise(resolve=>{finish=resolve;}):{servers:[copy(fixture),copy(other)]}):{});window.pywebview={api:{call}};render(<McpSettings notify={vi.fn()}/>);
    await userEvent.click(await screen.findByRole('button',{name:'Tools for Synthetic tools'}));await waitFor(()=>expect(finish).toBeTruthy());await userEvent.click(screen.getByRole('button',{name:'Close dialog'}));await userEvent.click(screen.getByRole('button',{name:'Tools for Other tools'}));await screen.findByRole('switch',{name:'lookup'});await act(async()=>finish({servers:[copy(fixture)]}));expect(screen.queryByRole('switch',{name:'write'})).toBeNull();expect(screen.getByRole('switch',{name:'lookup'}).getAttribute('aria-checked')).toBe('true');
  });
  it('does not replace another open connection with a closed view’s late save response',async()=>{
    let complete!:(value:unknown)=>void;const other={...fixture,id:'other-tools',name:'Other tools',tools:[{name:'lookup'}],enabled_tools:['lookup']};const call=vi.fn(async(action:string,data:Record<string,unknown>={})=>action==='mcp_servers'?{servers:[copy(fixture),copy(other)]}:action==='mcp_save'?new Promise(resolve=>{complete=resolve;}):{});window.pywebview={api:{call}};render(<McpSettings notify={vi.fn()}/>);await open();await userEvent.click(screen.getByRole('switch',{name:'write'}));await waitFor(()=>expect(complete).toBeTruthy());await userEvent.click(screen.getByRole('button',{name:'Close dialog'}));await userEvent.click(screen.getByRole('button',{name:'Tools for Other tools'}));await screen.findByRole('switch',{name:'lookup'});
    await act(async()=>complete({ok:true,server:{...fixture,tools:[{name:'different-old-catalog'}],enabled_tools:[]}}));expect(screen.queryByRole('switch',{name:'different-old-catalog'})).toBeNull();expect(screen.getByRole('switch',{name:'lookup'}).getAttribute('aria-checked')).toBe('true');expect(screen.getByRole('dialog').getAttribute('aria-labelledby')).toBeTruthy();
  });
});
