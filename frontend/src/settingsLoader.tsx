import { Component, lazy, Suspense, useLayoutEffect, useMemo, useRef, useState, type ComponentType, type ReactNode } from 'react';
import type { SettingsPage } from './settings';
import { updateReadiness, useUpdateReadiness } from './updateReadiness';
import { buildId, mapUrl } from 'virtual:forge-settings-entry';
import { retrySettingsAsset, SETTINGS_RETRY_LIMIT, SettingsLoadError, type SettingsLoadFailureCategory } from './settingsAssets';
import './settingsLoader.css';

type SettingsProps = React.ComponentProps<typeof SettingsPage>;
type SettingsModule = { default: ComponentType<SettingsProps> };
type Loader = () => Promise<SettingsModule>;

let failed = false, retries = 0;
let ready: SettingsModule | undefined;
export const loadSettingsPage: Loader = async () => {
  if (ready) return ready;
  const retrying = failed;
  if (retrying && retries >= SETTINGS_RETRY_LIMIT) throw new SettingsLoadError('retry_exhausted');
  if (retrying) retries += 1;
  try {
    const module = retrying && mapUrl ? await retrySettingsAsset(mapUrl, buildId, retries) : await import('./settings');
    if (typeof module.SettingsPage !== 'function') throw new Error();
    failed = false;
    ready = { default: module.SettingsPage };
    return ready;
  } catch {
    failed = true;
    throw new SettingsLoadError(retrying && retries >= SETTINGS_RETRY_LIMIT ? 'retry_exhausted' : 'load_failed');
  }
};

class SettingsBoundary extends Component<{ children: ReactNode; fallback: (category: SettingsLoadFailureCategory) => ReactNode }, { failure: SettingsLoadFailureCategory | null }> {
  state: { failure: SettingsLoadFailureCategory | null } = { failure: null };
  static getDerivedStateFromError(error: unknown) {
    return { failure: error instanceof SettingsLoadError ? error.category : 'load_failed' };
  }
  render() { return this.state.failure ? this.props.fallback(this.state.failure) : this.props.children; }
}

function Recovery({ failed, exhausted, waiting, locked, retry, onReturn, returnLabel }: { failed?: boolean; exhausted?: boolean; waiting?: boolean; locked: boolean; retry: () => void; onReturn: () => void; returnLabel: string }) {
  const focus = useRef<HTMLHeadingElement>(null);
  useLayoutEffect(() => {
    const active = document.activeElement;
    if (active === document.body || active?.closest('.settings-page-load') || active?.matches('button.nav-item.active')) focus.current?.focus();
  }, []);
  return <section className="settings-page-load empty-state" aria-label="Settings" onKeyDown={event => {
    if (event.key === 'Escape' && !locked) { event.preventDefault(); event.stopPropagation(); onReturn(); }
  }}>
    <h2 ref={focus} tabIndex={-1}>{failed ? 'Settings could not be opened' : waiting ? 'Settings is waiting' : 'Loading Settings…'}</h2>
    <p role={failed ? 'alert' : 'status'}>{exhausted ? 'Your chat is still available. Reopen Forge when you’re ready to try Settings again.' : failed ? 'Try again, or return to your conversation.' : waiting ? 'Finish the restart check before opening Settings.' : 'Your conversation stays available while this page loads.'}</p>
    <div className="settings-page-load-actions">
      {failed && !exhausted && <button className="primary" disabled={locked} onClick={retry}>Retry Settings</button>}
      <button className="secondary" disabled={locked} onClick={onReturn}>{returnLabel}</button>
    </div>
  </section>;
}

function SettingsContent({ Page, pageProps, committed }: { Page: ComponentType<SettingsProps>; pageProps: SettingsProps; committed: React.RefObject<boolean> }) {
  const root = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    committed.current = true;
    // A delayed page must not take focus back from navigation elsewhere.
    if (updateReadiness.canEdit() && (document.activeElement === document.body || document.activeElement?.closest('.settings-page-load'))) root.current?.querySelector<HTMLInputElement>('input[aria-label="Find a setting"]')?.focus();
  }, [committed]);
  return <div ref={root}><Page {...pageProps} /></div>;
}

/** Only the requested Settings page suspends; App owns the live workspace. */
export function SettingsPageLoader({ load, pageProps, onReturn, returnLabel = 'Return to chat' }: { load: Loader; pageProps: SettingsProps; onReturn: () => void; returnLabel?: string }) {
  const [attempt, setAttempt] = useState(0);
  const committed = useRef(false);
  const state = useUpdateReadiness();
  const locked = ['checking', 'locked', 'prepared', 'aborting'].includes(state.phase);
  const Page = useMemo(() => lazy(load), [load, attempt]);
  const retry = () => { if (updateReadiness.canEdit()) { committed.current = false; setAttempt(previous => previous + 1); } };
  const recovery = (category: SettingsLoadFailureCategory) => <Recovery failed exhausted={category === 'retry_exhausted'} locked={locked} retry={retry} onReturn={onReturn} returnLabel={returnLabel} />;
  // Do not introduce Settings effects or readiness owners during a saved-work check.
  // An already mounted page remains mounted and is fenced by the workspace.
  if (locked && !committed.current) return <Recovery waiting locked retry={retry} onReturn={onReturn} returnLabel={returnLabel} />;
  return <SettingsBoundary key={attempt} fallback={recovery}>
    <Suspense fallback={<Recovery locked={locked} retry={retry} onReturn={onReturn} returnLabel={returnLabel} />}>
      <SettingsContent Page={Page} pageProps={pageProps} committed={committed} />
    </Suspense>
  </SettingsBoundary>;
}
