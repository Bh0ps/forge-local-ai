import { useId, useLayoutEffect, useRef, useState } from 'react';
import { ArrowDown, Brain, Check } from 'lucide-react';
import './thinkingDisclosure.css';

const END_TOLERANCE = 24;
export interface ThinkingReadIntent {
  key: string;
  current?: { open: boolean; following: boolean; scrollTop: number; latestAvailable: boolean };
  focusAuthority?: { revision: number };
  restoreFocusRevision?: number;
}

/** Follow a live thought until the reader chooses an earlier part of it. */
export function ThinkingDisclosure({ text, streaming, active = streaming, intent }: { text: string; streaming: boolean; active?: boolean; intent?: ThinkingReadIntent }) {
  const saved = intent?.current;
  const [open, setOpen] = useState(saved?.open ?? streaming);
  const [latestAvailable, setLatestAvailable] = useState(saved?.latestAvailable ?? false);
  const latest = useRef(saved?.latestAvailable ?? false);
  const following = useRef(saved?.following ?? streaming);
  const position = useRef(saved?.scrollTop ?? 0);
  const restore = useRef(Boolean(saved));
  const automaticScroll = useRef<number | null>(null);
  const userToggle = useRef(false);
  const previous = useRef({ text, streaming });
  const details = useRef<HTMLDetailsElement>(null);
  const scroll = useRef<HTMLDivElement>(null);
  const regionId = useId();

  function showLatest(value: boolean) { latest.current = value; setLatestAvailable(value); }
  function remember(expanded = details.current?.open ?? open, explicit = false) {
    // Default streaming visibility must not open unread historical messages.
    if (!intent || !explicit && !intent.current) return;
    intent.current = { open: expanded, following: following.current, scrollTop: position.current, latestAvailable: latest.current };
  }
  function moveScroll(value: number) {
    if (!scroll.current) return;
    scroll.current.scrollTop = value;
    position.current = Math.max(0, Math.min(scroll.current.scrollTop, 10_000_000));
    automaticScroll.current = scroll.current.scrollTop;
  }
  function followLatest(focus = false, explicit = false) {
    following.current = true;
    showLatest(false);
    if (scroll.current) {
      moveScroll(scroll.current.scrollHeight);
      if (focus) scroll.current.focus({ preventScroll: true });
    }
    remember(undefined, explicit);
  }
  function activateSummary() {
    userToggle.current = true;
    // Native toggle events are queued; promotion can remove this node first.
    const expanded = !details.current?.open;
    if (expanded && streaming) { following.current = true; showLatest(false); }
    remember(expanded, true);
  }

  useLayoutEffect(() => {
    const revision = intent?.restoreFocusRevision;
    if (intent) delete intent.restoreFocusRevision;
    if (revision !== undefined && intent?.focusAuthority?.revision === revision && !document.hidden && document.hasFocus() && document.activeElement === document.body && open) scroll.current?.focus({ preventScroll: true });
    return () => {
      if (intent?.focusAuthority && scroll.current && !document.hidden && document.hasFocus() && document.activeElement === scroll.current) intent.restoreFocusRevision = intent.focusAuthority.revision;
    };
  }, []);

  useLayoutEffect(() => {
    if (restore.current) {
      restore.current = false;
      if (open && scroll.current) moveScroll(following.current ? scroll.current.scrollHeight : position.current);
    }
    const replaced = streaming && (!text.startsWith(previous.current.text) || !previous.current.streaming);
    const changed = text !== previous.current.text;
    previous.current = { text, streaming };
    if (replaced) {
      following.current = streaming;
      showLatest(false);
      setOpen(streaming);
    }
    if ((replaced ? streaming : open) && following.current && scroll.current) {
      moveScroll(scroll.current.scrollHeight);
    } else if (changed && !following.current) {
      showLatest(true);
    }
    remember();
  }, [text, streaming, open]);

  useLayoutEffect(() => {
    if (!scroll.current || typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(() => {
      if (details.current?.open && following.current && scroll.current) {
        moveScroll(scroll.current.scrollHeight);
        remember();
      }
    });
    observer.observe(scroll.current);
    return () => observer.disconnect();
  }, []);

  return <details ref={details} className={`reasoning thinking-disclosure${active ? ' thinking-active' : ''}`} open={open} onToggle={event => {
    const expanded = event.currentTarget.open;
    setOpen(expanded);
    const explicit = userToggle.current; userToggle.current = false;
    if (expanded && !open) {
      if (streaming) followLatest();
      else if (intent?.current && scroll.current) moveScroll(following.current ? scroll.current.scrollHeight : position.current);
    }
    remember(expanded, explicit);
  }}>
    <summary aria-controls={regionId} onClick={activateSummary} onKeyDown={event => {
      if (event.key === 'Enter' || event.key === ' ') activateSummary();
    }}>
      <span className="thinking-indicator" aria-hidden="true">{active ? <Brain size={14} /> : <Check size={14} />}</span>
      <span className="thinking-label">{active ? 'Thinking' : 'Model reasoning'}</span>
    </summary>
    <div className="thinking-frame">
      <div ref={scroll} id={regionId} className="thinking-scroll" role="region" aria-label="Model reasoning" tabIndex={0} onFocus={() => remember(undefined, true)} onScroll={event => {
        const element = event.currentTarget;
        const automatic = automaticScroll.current !== null && Math.abs(element.scrollTop - automaticScroll.current) < 1;
        if (!automatic) automaticScroll.current = null;
        position.current = Math.max(0, Math.min(element.scrollTop, 10_000_000));
        const atEnd = element.scrollHeight - element.scrollTop - element.clientHeight <= END_TOLERANCE;
        following.current = atEnd;
        if (atEnd) showLatest(false);
        else if (streaming) showLatest(true);
        remember(undefined, !automatic);
      }}>{text}</div>
      {open && latestAvailable && <button type="button" className="thinking-latest" onClick={() => followLatest(true, true)}><ArrowDown size={12} aria-hidden="true" />Jump to latest reasoning</button>}
    </div>
  </details>;
}
