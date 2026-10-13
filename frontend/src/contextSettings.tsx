import { useId } from 'react';
import { tokenLabel } from './api';
import { capacityLabel, contextBands, reasoningLabel, useContextMetadata } from './contextPresentation';
import { useContextSave, type ContextSaveState } from './contextSave';
import type { Settings } from './types';
import { Toggle } from './components';
import './contextSettings.css';

type Props = { settings: Settings; onChange: (delta: Partial<Settings>) => Promise<void>; onReasoningChange?: (delta:Partial<Settings>)=>Promise<void> };
export function ContextSettings(props: Props) {
  const save = useContextSave(props.settings, props.onChange), support = useContextMetadata(props.settings);
  return <>{props.onReasoningChange && <Toggle label="Model reasoning" description={support.metadata.controlSupported===false?'This engine controls reasoning. Your saved preference is retained.':'Request and display reasoning when the model supports this control.'} checked={props.settings.thinking} disabled={support.metadata.controlSupported===false} onChange={thinking=>void props.onReasoningChange!({thinking})}/>}<ContextFields settings={props.settings} save={save} support={support} /></>;
}
export function ContextFields({ settings, save, support }: { settings: Settings; save: ContextSaveState; support: ReturnType<typeof useContextMetadata> }) {
  const id = useId(), limit = support.metadata.limit;
  return <div className="context-setting context-refined"><label htmlFor={id}>Context window<output>{tokenLabel(save.value)}</output></label>
    <div className="context-presets" role="group" aria-label="Context presets">{contextBands.map(preset => <button type="button" data-context-accent={preset.accent} className={preset.tokens === save.value ? 'selected' : ''} aria-pressed={preset.tokens === save.value} aria-label={`${preset.name} ${tokenLabel(preset.tokens)}`} title={preset.description} key={preset.name} onClick={() => { save.preview(preset.tokens); void save.commit(); }}><span>{preset.name}</span><strong>{tokenLabel(preset.tokens)}</strong><small>{preset.description}</small></button>)}</div>
    <input id={id} type="range" min="2048" max="262144" step="2048" value={save.value} aria-valuetext={capacityLabel(save.value)} onChange={event => save.preview(Number(event.target.value))} onPointerUp={() => void save.commit()} onKeyUp={() => void save.commit()} onBlur={() => void save.commit()} />
    <div className="range-labels"><span>2K</span><span>128K</span><span>256K</span></div>
    <p className="context-support">{reasoningLabel(settings.thinking, support.metadata)}<br />{limit ? `${support.metadata.limitSource==='configured'?'Configured engine limit':support.metadata.limitSource==='loaded_instance'?'Loaded instance limit':'Selected model reports up to'} ${tokenLabel(limit)} context.` : 'Model context capacity has not been reported.'} Larger windows use more memory.{support.metadata.maximum && <span className="context-advertised-fact">Advertised maximum: {tokenLabel(support.metadata.maximum)}. Loaded capacity can be smaller.</span>}{support.metadata.instances?.slice(0,3).map(instance=><span className="context-advertised-fact" key={instance.model}>{instance.model} · {typeof instance.context_length==='number' && instance.context_length>0?`${tokenLabel(instance.context_length)} loaded context`:'Loaded context not reported'}</span>)}{support.metadata.instances && support.metadata.instances.length>3 && <span className="context-advertised-fact">More loaded instances are available in engine setup.</span>}{support.failed && <button className="text-button" type="button" onClick={support.retry}>Retry model details</button>}</p>
    {limit && save.value > limit ? <p className="context-capability-warning" role="status">This selection exceeds the model’s reported {tokenLabel(limit)} limit. Choose a supported window before prompting.</p> : null}
    {save.failed ? <div className="context-save-state" role="alert"><span>{save.pending?'Waiting for the earlier context request.':save.reconcile?'Confirm your unsaved context selection in this connection.':'Context could not be saved.'}</span><button type="button" className="text-button" disabled={save.pending} onClick={save.retry}>Retry context save</button><button type="button" className="text-button" disabled={save.pending} onClick={save.discard}>Discard context change</button></div> : save.pending || save.value !== save.confirmed ? <p className="context-save-state" role="status">Saving context…</p> : null}
  </div>;
}
