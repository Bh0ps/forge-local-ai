import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { loadSettingsPage, SettingsPageLoader } from '../settingsLoader';
import { DEFAULT_SETTINGS } from '../types';
import { retrySettingsAsset, SettingsLoadError } from '../settingsAssets';

vi.mock('virtual:forge-settings-entry', () => ({ buildId: 'a'.repeat(64), mapUrl: '/assets/forge-settings-entry.json' }));
vi.mock('../settings', () => { throw new Error('Synthetic private diagnostic marker'); });
vi.mock('../settingsAssets', async importOriginal => ({ ...await importOriginal<typeof import('../settingsAssets')>(), retrySettingsAsset: vi.fn() }));

describe('Settings retry session', () => {
  it('offers three explicit attempts, then keeps exhaustion truthful across reopening even after the asset is repaired', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    const retryAsset = vi.mocked(retrySettingsAsset).mockRejectedValue(new Error('Synthetic private URL and diagnostic marker'));
    const onReturn = vi.fn();
    const pageProps = { settings: DEFAULT_SETTINGS, onChange: vi.fn(), applyRecommendation: vi.fn(), models: [], refreshModels: vi.fn(), tab: 'general', setTab: vi.fn(), capabilities: {}, notify: vi.fn() };
    const view = render(<SettingsPageLoader load={loadSettingsPage} pageProps={pageProps} onReturn={onReturn} />);
    await screen.findByRole('heading', { name: 'Settings could not be opened' });
    expect(retryAsset).not.toHaveBeenCalled();
    for (let attempt = 1; attempt <= 3; attempt += 1) {
      const retry = screen.getByRole('button', { name: 'Retry Settings' });
      retry.focus(); await userEvent.keyboard('{Enter}');
      await waitFor(() => expect(retryAsset).toHaveBeenCalledTimes(attempt));
      await screen.findByRole('heading', { name: 'Settings could not be opened' });
      expect(retryAsset).toHaveBeenLastCalledWith('/assets/forge-settings-entry.json', 'a'.repeat(64), attempt);
    }
    expect(screen.queryByRole('button', { name: 'Retry Settings' })).toBeNull();
    expect(screen.getByRole('alert').textContent).toContain('Your chat is still available. Reopen Forge');
    expect(screen.queryByText(/Synthetic private/)).toBeNull();
    expect(screen.getByRole('heading', { name: 'Settings could not be opened' })).toBe(document.activeElement);
    await userEvent.keyboard('{Escape}'); expect(onReturn).toHaveBeenCalledTimes(1);
    retryAsset.mockResolvedValue({ SettingsPage: () => <h1>Repaired Settings</h1> });
    view.unmount();
    render(<SettingsPageLoader load={loadSettingsPage} pageProps={pageProps} onReturn={onReturn} />);
    await screen.findByRole('heading', { name: 'Settings could not be opened' });
    expect(screen.queryByRole('button', { name: 'Retry Settings' })).toBeNull();
    expect(screen.queryByRole('heading', { name: 'Repaired Settings' })).toBeNull();
    expect(screen.getByRole('alert').textContent).toBe('Your chat is still available. Reopen Forge when you’re ready to try Settings again.');
    expect(retryAsset).toHaveBeenCalledTimes(3);
    await act(async () => {
      await expect(loadSettingsPage()).rejects.toMatchObject({ category: 'retry_exhausted', message: 'Settings could not be opened.' });
    });
    await userEvent.click(screen.getByRole('button', { name: 'Return to chat' })); expect(onReturn).toHaveBeenCalledTimes(2);
    expect(new SettingsLoadError('retry_exhausted').message).not.toContain('Synthetic');
  });
});
