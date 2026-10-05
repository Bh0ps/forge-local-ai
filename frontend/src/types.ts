export interface Project { id: string; name: string; path: string; branch?: string; worktree?: string; }
export interface Chat { id: string; project_id: string | null; title: string; model: string; updated_at?: string; archived?: boolean; }
export interface Message { id?: string | number; role: string; content: string; thinking?: string; images?: string[]; tool_name?: string; status?: string; tool_calls?: unknown[]; sources?: { title?: string; url: string }[]; }
export interface SavedChat extends Chat { messages: Message[]; total_messages?: number; summary?: string; }
export interface Model { name: string; size?: number; details?: Record<string, unknown>; capabilities?: string[]; context_length?: number; max_context?: number; provider?: string; }
export interface Settings {
  context: number; tokens: number; temperature: number; thinking: boolean;
  theme: 'system' | 'light' | 'dark'; permission_profile: 'always_ask' | 'full_access' | 'deny_access';
  model: string; web: boolean; allow_edits: boolean; performance: string; startup: boolean; num_thread?: number;
  timezone: string; [key: string]: unknown;
}
export interface Run { id: string; chat_id?: string; project_id?: string; created_at?: string; status?: string; mode?: string; recovery?: string; model?: string; text?: string; request?: string; settings?: Partial<Settings>; parent_id?: string; goal_id?: string; cursor?: number; }
export interface RunEvent { seq?: number; type: string; text?: string; name?: string; state?: string; number?: number; result?: unknown; arguments?: unknown; approval_id?: string; command?: string; cancelled?: boolean; reason?: string; [key: string]: unknown; }
export interface Approval { job_id: string; approval_id: string; name: string; arguments?: unknown; command?: string; }
export interface Goal { id: string; title?: string; text?: string; status: string; markdown?: string; path?: string; tasks?: Task[]; next_action?: string; }
export interface Task { id: string; title: string; status: string; }
export interface Agent { id: string; name: string; instructions: string; model?: string; context?: number; enabled?: boolean; tools?: string[]; skills?: string[]; [key: string]: unknown; }
export interface Schedule { id: string; name: string; prompt: string; project_id?: string; agent_id?: string; timezone?: string; recurrence?: string; enabled?: boolean; next_run?: string; [key: string]: unknown; }
export interface Space { id: string; name: string; description?: string; project_ids?: string[]; notes?: string; }
export interface Connection { id: string; name: string; url?: string; command?: string; transport?: string; enabled?: boolean; status?: string; tools?: unknown[]; error?: string; }
export interface Plugin { id?: string; name: string; description?: string; version?: string; enabled?: boolean; source?: string; compatibility?: string; skills?: unknown[]; mcp_servers?: unknown[]; }
export interface UsageCount { input_tokens: number; output_tokens: number; cached_input_tokens?: number; requests: number; tokens_per_second?: number; }
export interface Usage { day?: UsageCount; month?: UsageCount; all_time?: UsageCount; daily?: { date: string; input_tokens: number; output_tokens: number }[]; models?: unknown[]; [key: string]: unknown; }
export type Page = 'chat' | 'projects' | 'spaces' | 'scheduled' | 'plugins' | 'agents' | 'usage' | 'memory' | 'settings';
export const DEFAULT_SETTINGS: Settings = { context: 32768, tokens: 4096, temperature: 0.3, thinking: true, theme: 'system', permission_profile: 'always_ask', model: '', web: true, allow_edits: true, performance: 'balanced', startup: false, timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC' };
