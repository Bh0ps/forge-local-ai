const asset = /^[A-Za-z0-9_-]+\.css$/;
const entry = /^forge-settings-[A-Za-z0-9_-]+\.js$/;
const fixedFailure = () => new Error('Settings could not be opened.');

export function settingsAssetMap(value: unknown, buildId: string, mapUrl: string, base: string) {
  const map = new URL(mapUrl, base), origin = new URL(base);
  const expected = new URL('./assets/forge-settings-entry.json', origin);
  if (!['http:', 'https:'].includes(origin.protocol) || map.origin !== origin.origin || map.pathname !== expected.pathname || map.search || map.hash || map.username || map.password) throw fixedFailure();
  if (!value || typeof value !== 'object') throw fixedFailure();
  const data = value as Record<string, unknown>;
  if (data.format !== 1 || data.build_id !== buildId || data.namespace !== 'settings_exports' || !/^[a-f0-9]{64}$/.test(buildId) || typeof data.entry !== 'string' || !entry.test(data.entry) || !Array.isArray(data.styles) || data.styles.length > 8 || !data.styles.every(style => typeof style === 'string' && asset.test(style))) throw fixedFailure();
  return { entry: new URL(data.entry, map), styles: [...new Set(data.styles as string[])].map(style => new URL(style, map)) };
}

async function stylesheet(url: URL, retry: number) {
  // Keep a successfully loaded stylesheet and its original cascade order.
  if ([...document.styleSheets].some(sheet => {
    if (!sheet.href) return false;
    const existing = new URL(sheet.href);
    // Chromium can retain an empty sheet after a failed preload.
    try { return existing.origin === url.origin && existing.pathname === url.pathname && sheet.cssRules.length > 0; }
    catch { return false; }
  })) return;
  const href = new URL(url); href.searchParams.set('forge_settings_retry', String(retry));
  await new Promise<void>((resolve, reject) => {
    const link = document.createElement('link'); link.rel = 'stylesheet'; link.href = href.href;
    const timeout = setTimeout(() => finish(false), 8000);
    const finish = (ok: boolean) => { clearTimeout(timeout); link.onload = link.onerror = null; if (!ok) link.remove(); ok ? resolve() : reject(fixedFailure()); };
    link.onload = () => finish(true); link.onerror = () => finish(false); document.head.append(link);
  });
}

export async function retrySettingsAsset(mapUrl: string, buildId: string, retry: number) {
  if (!Number.isSafeInteger(retry) || retry < 1 || retry > 3) throw fixedFailure();
  // Validate the trusted map URL before issuing any request.
  settingsAssetMap({ format: 1, build_id: buildId, namespace:'settings_exports', entry: 'forge-settings-check.js', styles: [] }, buildId, mapUrl, document.baseURI);
  const abort = new AbortController(), timer = setTimeout(() => abort.abort(), 8000);
  try {
    const response = await fetch(mapUrl, { cache: 'no-store', redirect: 'error', signal: abort.signal });
    if (!response.ok) throw fixedFailure();
    const text = await response.text(); if (text.length > 8192) throw fixedFailure();
    const assets = settingsAssetMap(JSON.parse(text), buildId, mapUrl, document.baseURI);
    await Promise.all(assets.styles.map(url => stylesheet(url, retry)));
    assets.entry.searchParams.set('forge_settings_retry', String(retry));
    const module = await import(/* @vite-ignore */ assets.entry.href);
    if (!module.settings_exports || typeof module.settings_exports.SettingsPage !== 'function') throw fixedFailure();
    return module.settings_exports;
  } catch { throw fixedFailure(); }
  finally { clearTimeout(timer); }
}
