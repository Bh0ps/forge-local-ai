import { describe, expect, it, vi } from 'vitest';
import { retrySettingsAsset, settingsAssetMap } from '../settingsAssets';
import { fireEvent, waitFor } from '@testing-library/react';

const build = 'a'.repeat(64), base = 'http://localhost:8080/workspace/index.html';
const map = 'http://localhost:8080/workspace/assets/forge-settings-entry.json';
const data = { format: 1, build_id: build, namespace:'settings_exports', entry: 'forge-settings-AbCd1234.js', styles: ['forge-settings-Wxyz1234.css'] };

describe('same-build Settings retry assets', () => {
  it('resolves the packaged Settings entry and its styles in the same deployment', () => {
    const assets = settingsAssetMap({ ...data, styles: [...data.styles, ...data.styles] }, build, map, base);
    expect(assets.entry.href).toBe('http://localhost:8080/workspace/assets/forge-settings-AbCd1234.js');
    expect(assets.styles.map(style => style.href)).toEqual(['http://localhost:8080/workspace/assets/forge-settings-Wxyz1234.css']);
  });
  it.each([
    ['https://untrusted.invalid/assets/forge-settings-entry.json', base],
    ['http://localhost:8080/another/assets/forge-settings-entry.json', base],
    [map + '?new=1', base], [map + '#fragment', base],
    ['file:///workspace/assets/forge-settings-entry.json', 'file:///workspace/index.html'],
  ])('refuses an unexpected map before any fetch', async (url, page) => {
    const fetch = vi.spyOn(globalThis, 'fetch');
    expect(() => settingsAssetMap(data, build, url, page)).toThrow('Settings could not be opened.');
    await expect(retrySettingsAsset(url, build, 1)).rejects.toThrow(); expect(fetch).not.toHaveBeenCalled();
  });
  it.each([
    { ...data, build_id: 'b'.repeat(64) }, { ...data, format: 2 }, { ...data, namespace:'other_page' },
    { ...data, entry: '../forge-settings-AbCd1234.js' }, { ...data, entry: 'https://untrusted.invalid/forge-settings-AbCd1234.js' },
    { ...data, entry: 'different-page-AbCd1234.js' }, { ...data, entry: 'forge-settings-AbCd1234.js?extra=1' },
    { ...data, styles: ['../private.css'] }, { ...data, styles: ['https://untrusted.invalid/page.css'] },
    { ...data, styles: Array(9).fill('page.css') }, { ...data, styles: [1] },
  ])('refuses build drift or malformed asset names without exposing the payload', value => {
    expect(() => settingsAssetMap(value, build, map, base)).toThrow('Settings could not be opened.');
  });
  it.each([0, 4, Infinity, 1.5])('bounds explicit retry requests before any fetch', async count => {
    const fetch = vi.spyOn(globalThis, 'fetch');
    await expect(retrySettingsAsset(map, build, count)).rejects.toThrow('Settings could not be opened.');
    expect(fetch).not.toHaveBeenCalled();
  });
  it('reports a fixed typed exhaustion category before issuing a fourth asset request', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch');
    await expect(retrySettingsAsset(map, build, 4)).rejects.toMatchObject({ name: 'SettingsLoadError', category: 'retry_exhausted', message: 'Settings could not be opened.' });
    expect(fetch).not.toHaveBeenCalled();
  });
  it.each(['empty','unavailable'])('rechecks a %s sheet retained after a failed preload', async kind => {
    const localMap = new URL('./assets/forge-settings-entry.json',document.baseURI).href;
    const style = new URL(data.styles[0],localMap).href;
    const stale = {href:style,get cssRules(){if(kind==='unavailable')throw new Error('Synthetic private diagnostic marker');return [];} };
    vi.spyOn(document,'styleSheets','get').mockReturnValue([stale] as unknown as StyleSheetList);
    const fetch=vi.spyOn(globalThis,'fetch').mockResolvedValue({ok:true,text:async()=>JSON.stringify(data)} as Response);
    const pending=retrySettingsAsset(localMap,build,1);
    const rejected=expect(pending).rejects.toThrow('Settings could not be opened.');
    await waitFor(()=>expect(document.head.querySelector('link[href*="forge_settings_retry=1"]')).toBeTruthy());
    const link=document.head.querySelector('link[href*="forge_settings_retry=1"]')!;
    expect((link as HTMLLinkElement).href).toBe(style+'?forge_settings_retry=1');fireEvent.error(link);
    await rejected;expect(document.head.contains(link)).toBe(false);expect(fetch).toHaveBeenCalledTimes(1);
  });
});
