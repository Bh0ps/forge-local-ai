import type { Settings } from './types';
export interface NativeBridge {
  call(action: string, data?: Record<string, unknown>): Promise<unknown>;
  mode?(mode: string, expanded?: boolean): Promise<unknown>;
  pin?(value: boolean): Promise<unknown>;
  choose_project?(): Promise<unknown>;
  screenshot?(): Promise<unknown>;
  copy?(text: string): Promise<unknown>;
  dictate?(data?: Record<string, unknown>): Promise<unknown>;
}
declare global { interface Window { pywebview?: { api: NativeBridge }; } }
export const isNative = () => Boolean(window.pywebview?.api?.call);
let token = '';
export function setApiToken(value: string) { token = value; }
export async function api<T = Record<string, unknown>>(action: string, data: Record<string, unknown> = {}): Promise<T> {
  let result: unknown;
  if (isNative()) result = await window.pywebview!.api.call(action, data);
  else {
    const headers: Record<string, string> = { 'Content-Type': 'application/json' };
    if (token) headers.Authorization = `Bearer ${token}`;
    const response = await fetch(`/api/v1/${encodeURIComponent(action)}`, { method: 'POST', headers, body: JSON.stringify(data) });
    if (!response.ok) {
      let message = `Request failed (${response.status})`;
      try { const error = await response.json(); message = error.error || error.detail || message; } catch { /* Keep HTTP status. */ }
      throw new Error(message);
    }
    result = await response.json();
  }
  if (result && typeof result === 'object' && 'error' in result && (result as { error?: unknown }).error) throw new Error(String((result as { error: unknown }).error));
  return result as T;
}
export async function native<T>(method: keyof NativeBridge, ...args: unknown[]): Promise<T> {
  const fn = window.pywebview?.api?.[method];
  if (typeof fn !== 'function') throw new Error('This control requires the Windows desktop app.');
  const result = await (fn as (...values: unknown[]) => Promise<unknown>)(...args);
  if (result && typeof result === 'object' && 'error' in result) throw new Error(String((result as { error: unknown }).error));
  return result as T;
}
export async function saveSettings(delta: Partial<Settings>): Promise<Settings> { return api<Settings>('settings', delta); }
export const errorText = (error: unknown) => error instanceof Error ? error.message : String(error);
export const listFrom = <T>(result: unknown, key: string): T[] => Array.isArray(result) ? result as T[] : ((result as Record<string, unknown> | null)?.[key] as T[] || []);
export function friendlyModel(name: string): string { const segment = name.split('/').pop() || name; return segment.replace(/:latest$/, '').replace(/[-_]/g, ' '); }
export function tokenLabel(value: number) { return `${Math.round(value / 1024)}K`; }
export function compactNumber(value: number) { return new Intl.NumberFormat(undefined, { notation: 'compact', maximumFractionDigits: 1 }).format(value || 0); }
export function bytesLabel(value = 0) { return value ? `${(value / 1e9).toFixed(1)} GB` : ''; }
