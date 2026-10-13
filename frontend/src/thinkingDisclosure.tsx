import { useId, useLayoutEffect, useRef, useState } from 'react';
import { ArrowDown, Brain, Check } from 'lucide-react';
import './thinkingDisclosure.css';

const END_TOLERANCE = 24;

/** Follow a live thought until the reader chooses an earlier part of it. */
export function ThinkingDisclosure({ text, streaming, active = streaming }: { text: string; streaming: boolean; active?: boolean }) {
  const [open, setOpen] = useState(streaming);
  const [latestAvailable, setLatestAvailable] = useState(false);
  const following = useRef(streaming);
  const previous = useRef({ text, streaming });
  const details = useRef<HTMLDetailsElement>(null);
  const scroll = useRef<HTMLDivElement>(null);
  const regionId = useId();

  function followLatest(focus = false) {
    following.current = true;
    setLatestAvailable(false);
    if (scroll.current) {
      scroll.current.scrollTop = scroll.current.scrollHeight;
      if (focus) scroll.current.focus({ preventScroll: true });
    }
  }

  useLayoutEffect(() => {
    const replaced = streaming && (!text.startsWith(previous.current.text) || !previous.current.streaming);
    const changed = text !== previous.current.text;
    previous.current = { text, streaming };
    if (replaced) {
      following.current = streaming;
      setLatestAvailable(false);
      setOpen(streaming);
    }
    if ((replaced ? streaming : open) && following.current && scroll.current) {
      scroll.current.scrollTop = scroll.current.scrollHeight;
    } else if (changed && !following.current) {
      setLatestAvailable(true);
    }
  }, [text, streaming, open]);

  useLayoutEffect(() => {
    if (!scroll.current || typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(() => {
      if (details.current?.open && following.current && scroll.current) {
        scroll.current.scrollTop = scroll.current.scrollHeight;
      }
    });
    observer.observe(scroll.current);
    return () => observer.disconnect();
  }, []);

  return <details ref={details} className={`reasoning thinking-disclosure${active ? ' thinking-active' : ''}`} open={open} onToggle={event => {
    const expanded = event.currentTarget.open;
    setOpen(expanded);
    if (expanded && !open && streaming) followLatest();
  }}>
    <summary aria-controls={regionId}>
      <span className="thinking-indicator" aria-hidden="true">{active ? <Brain size={14} /> : <Check size={14} />}</span>
      <span className="thinking-label">{active ? 'Thinking' : 'Model reasoning'}</span>
    </summary>
    <div className="thinking-frame">
      <div ref={scroll} id={regionId} className="thinking-scroll" role="region" aria-label="Model reasoning" tabIndex={0} onScroll={event => {
        const element = event.currentTarget;
        const atEnd = element.scrollHeight - element.scrollTop - element.clientHeight <= END_TOLERANCE;
        following.current = atEnd;
        if (atEnd) setLatestAvailable(false);
        else if (streaming) setLatestAvailable(true);
      }}>{text}</div>
      {open && latestAvailable && <button type="button" className="thinking-latest" onClick={() => followLatest(true)}><ArrowDown size={12} aria-hidden="true" />Jump to latest reasoning</button>}
    </div>
  </details>;
}
