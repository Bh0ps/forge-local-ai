import { useState } from 'react';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ResizeHandle } from '../workspaceLayout';

afterEach(()=>{cleanup();vi.unstubAllGlobals();});
function frames(){
  let serial=0;const pending=new Map<number,FrameRequestCallback>();
  const request=vi.fn((callback:FrameRequestCallback)=>{pending.set(++serial,callback);return serial;}),cancel=vi.fn((id:number)=>{pending.delete(id);});
  vi.stubGlobal('requestAnimationFrame',request);vi.stubGlobal('cancelAnimationFrame',cancel);
  return {request,cancel,pending,flush:()=>act(()=>{const batch=[...pending.values()];pending.clear();batch.forEach(callback=>callback(0));})};
}
function pointer(target:HTMLElement,type:string,x:number,id=1,button=0){
  const event=new MouseEvent(type,{bubbles:true,clientX:x,button});Object.defineProperty(event,'pointerId',{value:id});fireEvent(target,event);
}
function capture(target:HTMLElement){
  const pointers=new Set<number>(),release=vi.fn((id:number)=>pointers.delete(id));
  Object.assign(target,{setPointerCapture:(id:number)=>pointers.add(id),hasPointerCapture:(id:number)=>pointers.has(id),releasePointerCapture:release});return {release,pointers};
}
describe('frame-coalesced pane resizing',()=>{
  it('publishes at most once per frame and flushes the final pointer position before release',()=>{
    const clock=frames(),change=vi.fn();render(<ResizeHandle label="Resize fixture" value={260} min={220} max={360} change={change}/>);
    const handle=screen.getByRole('separator'),grab=capture(handle);
    pointer(handle,'pointerdown',100);pointer(handle,'pointermove',110);pointer(handle,'pointermove',120);pointer(handle,'pointermove',130);
    expect(change).not.toHaveBeenCalled();expect(clock.pending.size).toBe(1);clock.flush();expect(change).toHaveBeenCalledExactlyOnceWith(290);
    pointer(handle,'pointermove',150);pointer(handle,'pointerup',180);
    expect(change.mock.calls).toEqual([[290],[340]]);expect(clock.pending.size).toBe(0);expect(grab.release).toHaveBeenCalledExactlyOnceWith(1);
    clock.flush();pointer(handle,'pointermove',190);expect(change).toHaveBeenCalledTimes(2);
  });
  it('flushes the latest known point on cancellation, ignores other pointers and clamps reversed resizing',()=>{
    const clock=frames(),change=vi.fn();render(<ResizeHandle label="Resize fixture" value={320} min={280} max={520} change={change} reverse/>);
    const handle=screen.getByRole('separator');capture(handle);
    pointer(handle,'pointerdown',400);pointer(handle,'pointermove',100,2);pointer(handle,'pointercancel',0,2);expect(clock.pending.size).toBe(0);
    pointer(handle,'pointermove',380);pointer(handle,'pointercancel',0);expect(change).toHaveBeenCalledExactlyOnceWith(340);expect(clock.pending.size).toBe(0);
    pointer(handle,'pointerdown',400);pointer(handle,'pointermove',1000);clock.flush();expect(change).toHaveBeenLastCalledWith(280);
    pointer(handle,'pointerup',1100);expect(change).toHaveBeenCalledTimes(2);
  });
  it('finishes lost capture and cancels pending frames without writes after unmount',()=>{
    const clock=frames(),change=vi.fn(),view=render(<ResizeHandle label="Resize fixture" value={260} min={220} max={360} change={change}/>);
    const handle=screen.getByRole('separator'),grab=capture(handle);
    pointer(handle,'pointerdown',100);pointer(handle,'pointermove',125);pointer(handle,'lostpointercapture',125);
    expect(change).toHaveBeenCalledExactlyOnceWith(285);expect(clock.pending.size).toBe(0);
    pointer(handle,'pointerdown',100);pointer(handle,'pointermove',140);view.unmount();clock.flush();
    expect(change).toHaveBeenCalledTimes(1);expect(grab.pointers.size).toBe(0);
  });
  it('uses the latest callback for pending movement and retains immediate keyboard controls',()=>{
    const clock=frames(),old=vi.fn(),change=vi.fn(),view=render(<ResizeHandle label="Resize fixture" value={260} min={220} max={360} change={old}/>);
    const handle=screen.getByRole('separator');capture(handle);pointer(handle,'pointerdown',100);pointer(handle,'pointermove',120);
    view.rerender(<ResizeHandle label="Resize fixture" value={260} min={220} max={360} change={change}/>);clock.flush();
    expect(old).not.toHaveBeenCalled();expect(change).toHaveBeenCalledExactlyOnceWith(280);pointer(handle,'pointerup',120);view.unmount();
    function Keyboard(){const [width,setWidth]=useState(320);return <ResizeHandle label="Keyboard resize" value={width} min={280} max={520} change={setWidth} reverse/>;}
    render(<Keyboard/>);const keyboard=screen.getByRole('separator');fireEvent.keyDown(keyboard,{key:'ArrowLeft'});expect(keyboard.getAttribute('aria-valuenow')).toBe('330');
    fireEvent.keyDown(keyboard,{key:'Home'});expect(keyboard.getAttribute('aria-valuenow')).toBe('280');fireEvent.keyDown(keyboard,{key:'End'});expect(keyboard.getAttribute('aria-valuenow')).toBe('520');
  });
});
