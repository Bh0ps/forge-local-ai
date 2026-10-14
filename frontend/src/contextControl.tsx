import { useEffect, useId, useRef } from 'react';
import { Brain } from 'lucide-react';
import { ContextFields } from './contextSettings';
import { capacityLabel, contextBand, reasoningLabel, useContextMetadata } from './contextPresentation';
import { useContextSave } from './contextSave';
import type { Settings } from './types';
import { useControlLabel } from './components';

export function ContextControl({ settings, onChange, open, setOpen }: { settings: Settings; onChange: (v: Partial<Settings>) => Promise<void>; open: boolean; setOpen: (v: boolean) => void }) {
  const ref = useRef<HTMLDivElement>(null), trigger = useRef<HTMLButtonElement>(null), id = useId();
  const close=useRef(setOpen);close.current=setOpen;
  const save = useContextSave(settings, onChange), support = useContextMetadata(settings), band = contextBand(save.value);
  const label = `Context controls · ${band.name} · ${capacityLabel(save.value)} · ${reasoningLabel(settings.thinking, support.metadata)}`;
  const hint = useControlLabel(label, !open);
  useEffect(() => {
    if (!open) return;
    (ref.current?.querySelector<HTMLButtonElement>('[aria-pressed="true"]') || ref.current?.querySelector<HTMLInputElement>('input[type="range"]'))?.focus();
    const dismiss = (event: PointerEvent) => { if (!ref.current?.contains(event.target as Node)) close.current(false); };
    const key = (event: KeyboardEvent) => { if (event.key === 'Escape') { const ownsFocus=ref.current?.contains(document.activeElement);event.preventDefault();event.stopPropagation();close.current(false);if(ownsFocus)trigger.current?.focus(); } };
    window.addEventListener('pointerdown', dismiss); window.addEventListener('keydown', key);
    return () => { window.removeEventListener('pointerdown', dismiss); window.removeEventListener('keydown', key); };
  }, [open]);
  return <div className="context-control" ref={ref}><button type="button" ref={trigger} className={`icon-button labeled-control context-trigger${open ? ' active' : ''}`} data-context-accent={band.accent} aria-label={label} aria-haspopup="dialog" aria-expanded={open} aria-controls={open ? id : undefined} {...hint.events} onClick={() => { hint.dismiss(); setOpen(!open); }}><Brain className="context-brain" size={17} aria-hidden="true" /><span className={`context-reasoning-mark${settings.thinking ? ' requested' : ''}`} aria-hidden="true" />{hint.content}</button>{open && <div id={id} className="context-popover" role="dialog" aria-label="Context window controls"><header><strong>Context window</strong><span>Configured capacity · reasoning is separate</span></header><ContextFields settings={settings} save={save} support={support} /></div>}</div>;
}
