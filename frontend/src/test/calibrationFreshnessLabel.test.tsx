import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen } from '@testing-library/react';
import { CalibrationSettings } from '../calibrationSettings';
import { DEFAULT_SETTINGS } from '../types';

afterEach(() => {delete window.pywebview;});
describe('approved calibration freshness label', () => {
  it.each([false, true, undefined])('reports stored approval separately from fresh eligibility (%s)', async calibrated => {
    const call = vi.fn(async (action:string) => {
      if (action !== 'performance_calibrations') throw new Error('Unexpected synthetic action');
      return {calibrations: [{id: 'profile', model: 'synthetic-model', context: 8192,
        validated: calibrated !== false, calibrated, median_total_seconds: 1, quality_scope: 'Synthetic probe'}]};
    });
    window.pywebview = {api: {call}};
    render(<CalibrationSettings settings={{...DEFAULT_SETTINGS, model: 'synthetic-model', adaptive_profile_ids: ['profile']}}
      onChange={async () => {throw new Error('Read-only label must not change settings');}} notify={() => {}}/>);
    expect(await screen.findByText(calibrated === false ? 'Approved · needs recheck' : 'Approved')).toBeTruthy();
    expect(screen.queryByRole('button', {name: 'Approve profile'})).toBeNull();
    expect(call.mock.calls.map(([action]) => action)).toEqual(['performance_calibrations']);
  });
  it.each([{validated:false}, {validated:true, calibrated:false}, {validated:false, calibrated:true}])('keeps stale or inconsistent approved intent honest (%j)', async fields => {
    window.pywebview = {api: {call: vi.fn(async () => ({calibrations: [{id:'profile',model:'synthetic-model',context:8192,
      median_total_seconds:1,quality_scope:'Synthetic probe',...fields}]}))}};
    render(<CalibrationSettings settings={{...DEFAULT_SETTINGS,model:'synthetic-model',adaptive_profile_ids:['profile']}}
      onChange={async()=>{}} notify={()=>{}}/>);
    expect(await screen.findByText('Approved · needs recheck')).toBeTruthy();
    expect(screen.queryByRole('button',{name:'Approve profile'})).toBeNull();
  });
  it.each([{}, {context:'unknown',median_total_seconds:'unknown'}, {context:-1,median_total_seconds:Infinity},
    {context:1e100,median_total_seconds:1e100}, {median_total_seconds:0}, {median_total_seconds:-1}])('renders invalid legacy measurements without losing Refresh (%j)', async fields => {
    const call=vi.fn(async()=>({calibrations:[{id:'profile',model:'synthetic-model',validated:false,calibrated:false,...fields}]}));
    window.pywebview={api:{call}};
    render(<CalibrationSettings settings={{...DEFAULT_SETTINGS,model:'synthetic-model',adaptive_profile_ids:['profile']}}
      onChange={async()=>{}} notify={()=>{}}/>);
    expect(await screen.findByText('Context unknown · Probe timing unavailable')).toBeTruthy();
    expect(screen.getByText('Approved · needs recheck')).toBeTruthy();
    fireEvent.click(screen.getByRole('button',{name:'Refresh profiles'}));
    await act(async()=>{});
    expect(call).toHaveBeenCalledTimes(2);
  });
  it.each(['thinking','tokens','temperature'] as const)('ignores older success and failure after %s changes', async field => {
    for (const outcome of ['success','failure'] as const) {
      let resolve!:(value:unknown)=>void,reject!:(error:Error)=>void;
      const old=new Promise((yes,no)=>{resolve=yes;reject=no;});let reads=0;
      const profile={id:'profile',model:'synthetic-model',context:8192,median_total_seconds:1,quality_scope:'Synthetic probe'};
      const call=vi.fn(async(action:string)=>{if(action!=='performance_calibrations')throw new Error('Unexpected synthetic action');
        return ++reads===1?old:{calibrations:[{...profile,validated:false,calibrated:false}]};});
      window.pywebview={api:{call}};
      const settings={...DEFAULT_SETTINGS,model:'synthetic-model',adaptive_profile_ids:['profile']};
      const props={settings,onChange:vi.fn(async()=>{}),notify:vi.fn()};
      const view=render(<CalibrationSettings {...props}/>);
      const value=field==='thinking'?!settings.thinking:field==='tokens'?(settings.tokens===4096?2048:4096):(settings.temperature===.7?.4:.7);
      view.rerender(<CalibrationSettings {...props} settings={{...settings,[field]:value}}/>);
      expect(await screen.findByText('Approved · needs recheck')).toBeTruthy();
      await act(async()=>{if(outcome==='success')resolve({calibrations:[{...profile,validated:true,calibrated:true}]});else reject(new Error('synthetic-private-old-failure'));});
      expect(screen.getByText('Approved · needs recheck')).toBeTruthy();
      expect(screen.queryByText('Approved')).toBeNull();expect(screen.queryByText(/synthetic-private/)).toBeNull();
      expect(props.onChange).not.toHaveBeenCalled();expect(props.notify).not.toHaveBeenCalled();
      expect(call.mock.calls.map(([action])=>action)).toEqual(['performance_calibrations','performance_calibrations']);
      view.unmount();
    }
  });
});
