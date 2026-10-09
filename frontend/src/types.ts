export interface Project { id: string; name: string; path: string; branch?: string; worktree?: string; }
export interface Chat { id: string; project_id: string | null; title: string; model: string; updated_at?: string; archived?: boolean; }
export interface Message { id?: string | number; role: string; content: string; thinking?: string; images?: string[]; tool_name?: string; status?: string; tool_calls?: unknown[]; interaction_run_id?: string; sources?: { title?: string; url: string }[]; }
export interface SavedChat extends Chat { messages: Message[]; total_messages?: number; summary?: string; }
export interface Model { name: string; size?: number; details?: Record<string, unknown>; capabilities?: string[]; context_length?: number; max_context?: number; provider?: string; }
export interface Settings {
  context: number; tokens: number; temperature: number; thinking: boolean;
  theme: 'system' | 'light' | 'dark'; permission_profile: 'always_ask' | 'full_access' | 'deny_access';
  model: string; web: boolean; allow_edits: boolean; performance: string; startup: boolean; num_thread?: number;
  timezone: string; [key: string]: unknown;
}
export interface GoalReview { status: 'reviewing' | 'complete' | 'needs_changes' | 'insufficient_evidence' | 'error' | 'disabled' | 'unavailable'; verdict?: 'complete' | 'needs_changes' | 'insufficient_evidence'; summary?: string; feedback?: string[]; review_run_id?: string; model?: string; attempt?: number; }
export interface ProgressSummary { activity?: string; phase?: string; verified?: number; total?: number; verified_task_ids?: string[]; verified_count?: number; requirement_count?: number; waiting_reason?: string; blocker?: string; next_action?: string; available_actions?: string[]; }
export interface PlannerAssignment { state: 'requested' | 'waiting' | 'consumed' | 'unavailable' | 'superseded'; scope_revision?: number; run_id?: string; requested_at?: string; consumed_at?: string; reason?: string; guidance_hash?: string; }
export interface Run { id: string; workflow_stage?: string; progress_summary?: ProgressSummary; planner_assignment?: PlannerAssignment; context_snapshot?: RunContextSnapshot; builder_revision?: number; result_consumed?: boolean; chat_id?: string; project_id?: string; created_at?: string; status?: string; mode?: string; recovery?: string; model?: string; text?: string; request?: string; request_message_id?: number; settings?: Partial<Settings>; parent_id?: string; goal_id?: string; cursor?: number; review?: GoalReview; }
export interface RunEvent { seq?: number; type: string; text?: string; name?: string; state?: string; number?: number; result?: unknown; arguments?: unknown; approval_id?: string; command?: string; cancelled?: boolean; reason?: string; [key: string]: unknown; }
export interface Approval { job_id: string; approval_id: string; name: string; arguments?: unknown; command?: string; }
export interface Goal { id: string; revision?: number; execution_mode?: string; progress_summary?: ProgressSummary; planner_assignment?: PlannerAssignment; builder_revision?: number; builder_id?: string; chat_id?: string; run_id?: string; title?: string; text?: string; status: string; markdown?: string; path?: string; tasks?: Task[]; next_action?: string; review?: GoalReview; }
export interface QuestionOption { label: string; description?: string; recommended?: boolean; }
export interface QuestionItem { id: string; header?: string; question: string; options: QuestionOption[]; allow_free_text?: boolean; }
export interface UserQuestion { id: string; run_id: string; chat_id: string; status: string; items: QuestionItem[]; }
export interface QuestionAnswer { option?: string; text?: string; }
export interface Task { id: string; requirement_id?: string; evidence?: (string | EvidenceRef)[]; title?: string; text?: string; status: string; }
export interface Agent { id: string; name: string; instructions: string; model?: string; context?: number; enabled?: boolean; tools?: string[]; skills?: string[]; [key: string]: unknown; }
export interface Schedule { id: string; name: string; prompt: string; project_id?: string; agent_id?: string; timezone?: string; recurrence?: string; enabled?: boolean; next_run?: string; [key: string]: unknown; }
export interface Space { id: string; name: string; description?: string; project_ids?: string[]; notes?: string; }
export interface Connection { id: string; name: string; url?: string; command?: string; transport?: string; enabled?: boolean; status?: string; tools?: unknown[]; error?: string; }
export interface Plugin { id?: string; name: string; description?: string; version?: string; enabled?: boolean; source?: string; compatibility?: string; skills?: unknown[]; mcp_servers?: unknown[]; }
export interface UsageCount { input_tokens: number; output_tokens: number; cached_input_tokens?: number; requests: number; tokens_per_second?: number; }
export interface Usage { day?: UsageCount; month?: UsageCount; all_time?: UsageCount; daily?: { date: string; input_tokens: number; output_tokens: number }[]; models?: unknown[]; [key: string]: unknown; }
export type Page = 'chat' | 'builder' | 'projects' | 'spaces' | 'scheduled' | 'plugins' | 'agents' | 'usage' | 'memory' | 'settings';
export interface EvidenceRef { source: 'session' | 'preview' | 'artifact' | 'manual'; reference?: string; sha256?: string; note?: string; }
export interface QualityGate { status: 'pending' | 'passed' | 'failed' | 'not_applicable'; evidence: EvidenceRef[]; }
export interface BuildBrief { id: string; revision: number; project_id: string; chat_id?: string; goal_id?: string; title: string; objective: string; audience: string; constraints: string; requirements: {id: string; text: string; acceptance: string}[]; gates: Record<string,QualityGate>; }
export interface PreviewSession { id: string; project_id: string; builder_id?: string; status: string; url?: string | null; mode?: string; session_id?: string; }
export interface PhaseTiming { queue_seconds?: number | null; load_seconds?: number | null; prefill_seconds?: number | null; first_output_seconds?: number | null; first_visible_seconds?: number | null; decode_seconds?: number | null; total_seconds?: number; provenance?: Record<string,string>; }
export interface RunContextSnapshot { version?: number; token_breakdown?: Record<string,number>; estimated_tokens?: number; tools?: string[]; skills?: string[]; [key: string]: unknown; }
export interface VerificationFeedback { scope_count: number; changed_since_check: boolean; repeated_unchanged: boolean; failures: {tool: string; kind: string; failed: {name: string; detail?: string}[]; failed_count: number; invocation_id?: string; artifact_id?: string; repeated?: number; changed_since_check?: boolean}[]; }
export interface SkillManifest { schema_version: 1; version?: string; bundle?: string; phases?: string[]; intents?: string[]; triggers?: string[]; exclude_triggers?: string[]; requires_tools?: string[]; recommended_tools?: string[]; resources?: string[]; }
export const DEFAULT_SETTINGS: Settings = { context: 32768, tokens: 4096, temperature: 0.3, thinking: true, theme: 'system', permission_profile: 'always_ask', model: '', web: true, allow_edits: true, performance: 'balanced', startup: false, timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC' };
