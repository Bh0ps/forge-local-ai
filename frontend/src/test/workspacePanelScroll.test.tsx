import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { WorkspacePanel, type WorkspacePanelFocusScope } from '../workspacePanel';

afterEach(()=>{cleanup();vi.unstubAllGlobals();vi.useRealTimers();});
function observeResizes(){
  const instances:FixtureResizeObserver[]=[];
  class FixtureResizeObserver {
    observed=new Set<Element>();
    constructor(private callback:ResizeObserverCallback){instances.push(this);}
    observe=(element:Element)=>{this.observed.add(element);};
    disconnect=vi.fn(()=>{this.observed.clear();});
    fire=()=>act(()=>this.callback([],this as unknown as ResizeObserver));
  }
  vi.stubGlobal('ResizeObserver',FixtureResizeObserver);return instances;
}
function pendingPane(tab='files'){
  const observers=observeResizes();
  const owner:WorkspacePanelFocusScope={page:'chat',projectId:'studio',chatId:'first',navigation:0,transport:window.fetch,visible:true,current:()=>true};
  const content=(active:string,text:string,scope=owner)=><><input aria-label="Outside pane"/><WorkspacePanel tab={active} select={()=>{}} close={()=>{}} width={320} resize={()=>{}} focusScope={scope}><div>{text}</div></WorkspacePanel></>;
  const view=render(content(tab,'Loaded entries'));
  const panel=screen.getByRole('tabpanel',{name:tab==='files'?'Files':'Context'});
  const metrics={height:1000,viewport:200,top:0};
  const clamp=(value:number)=>Math.max(0,Math.min(value,Math.max(0,metrics.height-metrics.viewport)));
  Object.defineProperties(panel,{
    scrollHeight:{configurable:true,get:()=>metrics.height},clientHeight:{configurable:true,get:()=>metrics.viewport},
    scrollTop:{configurable:true,get:()=>metrics.top=clamp(metrics.top),set:(value:number)=>{metrics.top=clamp(value);}},
  });
  panel.scrollTop=180;fireEvent.scroll(panel);
  view.rerender(content('activity','Other tab'));
  metrics.height=200;view.rerender(content(tab,'Loading entries'));
  expect(panel.scrollTop).toBe(0);expect(observers).toHaveLength(1);
  return {view,panel,metrics,observer:observers[0],owner,draw:(active=tab,text='Loaded entries',scope=owner)=>view.rerender(content(active,text,scope))};
}

describe('delayed detail-pane reading position restoration',()=>{
  it.each(['files','context'])('waits for actual %s content growth after native-style clamping and preserves outside focus',async tab=>{
    const fixture=pendingPane(tab),outside=screen.getByRole('textbox',{name:'Outside pane'});outside.focus();
    // A scroll event caused by the initial clamped assignment must not erase the target.
    fireEvent.scroll(fixture.panel);fixture.metrics.height=300;fixture.observer.fire();expect(fixture.panel.scrollTop).toBe(0);
    fixture.metrics.height=600;fixture.draw(tab,'Asynchronously loaded entries');
    await waitFor(()=>expect(fixture.panel.scrollTop).toBe(180));
    expect(document.activeElement).toBe(outside);expect(fixture.observer.observed.size).toBe(0);
    fixture.panel.scrollTop=70;fireEvent.scroll(fixture.panel);fixture.observer.fire();expect(fixture.panel.scrollTop).toBe(70);
  });
  it('also restores when existing content changes size without a DOM mutation',()=>{
    const fixture=pendingPane();fixture.metrics.height=600;fixture.observer.fire();expect(fixture.panel.scrollTop).toBe(180);
    expect(fixture.observer.observed.size).toBe(0);
  });
  it.each(['wheel','pointerdown','touchstart','keydown','scroll'])('abandons delayed movement on reader %s input',async input=>{
    const fixture=pendingPane();let expected=0;
    if(input==='scroll'){fixture.metrics.height=300;fixture.panel.scrollTop=40;expected=40;}
    fireEvent(fixture.panel,new Event(input,{bubbles:true}));
    fixture.metrics.height=600;await act(async()=>fixture.draw('files','Content arrived after reader input'));fixture.observer.fire();
    expect(fixture.panel.scrollTop).toBe(expected);expect(fixture.observer.observed.size).toBe(0);
    fixture.draw('activity');fixture.draw('files');expect(fixture.panel.scrollTop).toBe(expected);
  });
  it.each(['tab','scope','unmount'])('retires its observers and pending callback on %s change',boundary=>{
    const fixture=pendingPane();
    if(boundary==='tab')fixture.draw('goals');
    else if(boundary==='scope')fixture.draw('files','Other conversation',{...fixture.owner,chatId:'second',navigation:1});
    else fixture.view.unmount();
    fixture.metrics.height=600;fixture.observer.fire();expect(fixture.panel.scrollTop).toBe(0);expect(fixture.observer.observed.size).toBe(0);
  });
  it('bounds waiting and disconnects when content never becomes tall enough',()=>{
    vi.useFakeTimers();const fixture=pendingPane();act(()=>vi.advanceTimersByTime(5000));
    fixture.metrics.height=600;fixture.observer.fire();expect(fixture.panel.scrollTop).toBe(0);expect(fixture.observer.observed.size).toBe(0);
  });
});
