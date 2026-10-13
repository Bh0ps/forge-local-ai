import type { Settings } from './types';
import { updateReadiness } from './updateReadiness';
const readActions=new Set(('bootstrap commands projects chats get_chat attachment tasks runs poll plans plan_get unknown_actions goals goal_get spaces agents schedules usage providers provider_presets models show permission_overrides storage_info setup_status openrouter_status github_status github_repos channel_list channel_notifications memory_list memory_export update_status browser_status browser_native_status dictation_status runtime_status hf_status performance_status pending_questions skills skill_read skill_preview plugins catalogs catalog_discover integrations tools skills_search skills_resource_read skill_route_preview skill_versions builder_list builder_get builder_read build_brief_get builder_changes preview_status preview_logs preview_browser_status preview_browser_inspect preview_browser_capture preview_browser_diagnostics run_context_inspect usage_requests command_session_status command_session_read command_session_wait artifact_list artifact_get artifact_download document_list performance performance_benchmarks performance_calibrations draft_get submission_get builder_runtime_get ai_workflow_status ai_workflow_self_test_status ui_layout_get run_activity run_diagnostics_export host_status').split(' '));
const persistenceActions=new Set(['draft_save','draft_confirm','ui_layout_save']);
let pendingMutations=0;
updateReadiness.register({kind:'requests',inspect:()=>({reasons:pendingMutations ? ['submission_pending'] : [],pending:pendingMutations,state:pendingMutations?'waiting':'ready'}),flushAndConfirm:async()=>pendingMutations ? {ok:false,reason:'submission_pending'} : {ok:true,receipts:[]}});
function mutationAdmission(action:string,data:Record<string,unknown>) {
  if(action.startsWith('update_handoff_') || readActions.has(action) || action==='settings' && !Object.keys(data).length)return ()=>{};
  if(persistenceActions.has(action) && ['idle','failed','checking'].includes(updateReadiness.getSnapshot().phase))return ()=>{};
  if(!updateReadiness.canEdit())throw new Error('Forge is confirming a restart. Keep working after cancellation is confirmed.');
  pendingMutations++;updateReadiness.changed();let finished=false;
  return ()=>{if(!finished){finished=true;pendingMutations--;updateReadiness.changed();}};
}
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
const PROOF_KEY = 'forge.browser-client-proof.v1';
let memoryProof: {origin: string; value: string} | null = null;
function clientProof(): string {
  try {
    const stored = window.localStorage.getItem(PROOF_KEY);
    if (stored) {
      const record = JSON.parse(stored) as {origin?: string; value?: string};
      if (record.origin === window.location.origin && typeof record.value === 'string') return record.value;
    }
  } catch { /* Restricted browser storage: retain the current tab's proof. */ }
  return memoryProof?.origin === window.location.origin ? memoryProof.value : '';
}
function saveClientProof(value: string) {
  memoryProof = value ? {origin: window.location.origin, value} : null;
  try {
    if (memoryProof) window.localStorage.setItem(PROOF_KEY, JSON.stringify(memoryProof));
    else window.localStorage.removeItem(PROOF_KEY);
  } catch { /* Pairing still works in this tab when storage is disabled. */ }
}
export function setApiToken(value: string) { token = value; }
export async function api<T = Record<string, unknown>>(action: string, data: Record<string, unknown> = {}): Promise<T> {
  const finish=mutationAdmission(action,data);
  try {
  let result: unknown;
  if (isNative()) result = await window.pywebview!.api.call(action, data);
  else {
    const headers: Record<string, string> = { 'Content-Type': 'application/json' };
    if (token) headers.Authorization = `Bearer ${token}`;
    const proof = clientProof();
    if (proof) headers['X-Forge-Client-Proof'] = proof;
    const response = await fetch(`/api/v1/${encodeURIComponent(action)}`, { method: 'POST', headers, body: JSON.stringify(data), credentials: 'same-origin', redirect: 'error' });
    if (!response.ok) {
      let message = `Request failed (${response.status})`;
      try { const error = await response.json(); message = error.error || error.detail || message; } catch { /* Keep HTTP status. */ }
      throw new Error(message);
    }
    result = await response.json();
    if (action === 'pair' && result && typeof result === 'object' && 'ok' in result && result.ok === true && 'client_proof' in result
      && typeof result.client_proof === 'string' && result.client_proof.length >= 32 && result.client_proof.length <= 256) saveClientProof(result.client_proof);
    if (action === 'unpair' && result && typeof result === 'object' && 'ok' in result && result.ok) {
      // A delayed unpair response cannot erase credentials from a newer pairing.
      if (clientProof() === proof) { saveClientProof(''); token = ''; }
    }
  }
  if (result && typeof result === 'object' && 'error' in result && (result as { error?: unknown }).error) throw new Error(String((result as { error: unknown }).error));
  return result as T;
  } finally {finish();}
}
export async function native<T>(method: keyof NativeBridge, ...args: unknown[]): Promise<T> {
  const finish=method==='copy' ? ()=>{} : mutationAdmission(`native_${method}`,{});
  try {
  const fn = window.pywebview?.api?.[method];
  if (typeof fn !== 'function') throw new Error('This control requires the Windows desktop app.');
  const result = await (fn as (...values: unknown[]) => Promise<unknown>)(...args);
  if (result && typeof result === 'object' && 'error' in result) throw new Error(String((result as { error: unknown }).error));
  return result as T;
  } finally {finish();}
}
export async function saveSettings(delta: Partial<Settings>): Promise<Settings> { return api<Settings>('settings', delta); }
export const errorText = (error: unknown) => error instanceof Error ? error.message : String(error);
export const listFrom = <T>(result: unknown, key: string): T[] => Array.isArray(result) ? result as T[] : ((result as Record<string, unknown> | null)?.[key] as T[] || []);
export function friendlyModel(name: string): string { const segment = name.split('/').pop() || name; return segment.replace(/:latest$/, '').replace(/[-_]/g, ' '); }
export function tokenLabel(value: number) { return `${Math.round(value / 1024)}K`; }
export function compactNumber(value: number) { return new Intl.NumberFormat(undefined, { notation: 'compact', maximumFractionDigits: 1 }).format(value || 0); }
export function bytesLabel(value = 0) { return value ? `${(value / 1e9).toFixed(1)} GB` : ''; }
