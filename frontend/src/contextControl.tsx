import { useEffect, useRef } from 'react';
import { Brain } from 'lucide-react';
import { ContextSettings } from './contextSettings';
import { IconButton } from './components';
import { tokenLabel } from './api';
import type { Settings } from './types';

export function ContextControl({ settings, onChange, open, setOpen }: { settings: Settings; onChange: (v: Partial<Settings>) => Promise<void>; open: boolean; setOpen: (v: boolean) => void }) {
  const ref=useRef<HTMLDivElement>(null);
  useEffect(() => { if (!open) return; const dismiss=(e: PointerEvent) => { if (!ref.current?.contains(e.target as Node)) setOpen(false); }; const key=(e: KeyboardEvent) => { if (e.key==='Escape') setOpen(false); }; window.addEventListener('pointerdown',dismiss); window.addEventListener('keydown',key); return () => { window.removeEventListener('pointerdown',dismiss); window.removeEventListener('keydown',key); }; }, [open,setOpen]);
  return <div className="context-control" ref={ref}><IconButton label={`Context controls · ${tokenLabel(settings.context)}`} active={open} onClick={() => setOpen(!open)}><Brain size={17} /></IconButton>{open && <div className="context-popover" role="dialog" aria-label="Context window controls"><header><strong>Context window</strong><span>Capacity, not reasoning strength</span></header><ContextSettings settings={settings} onChange={onChange} /></div>}</div>;
}
