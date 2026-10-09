# Forge coordinator architecture

The Windows host starts one per-user coordinator guarded by a named mutex. A tray
process hosts the shared service and authenticated local HTTP API. The React
workspace and HUD use the same window and native bridge. Browser clients pair with
a short-lived, single-use code; their HttpOnly SameSite cookie authorizes versioned
APIs. Cursor polling and SSE replay the same persisted run events.

`ForgeService` owns `ForgeStore`, `RunManager`, `ProviderPool`, `ToolRegistry`,
`IntegrationHub` and the injected Windows broker. SQLite connections are per
operation, with WAL and ordered Forge migrations. Legacy schema/import methods are
retained for compatibility. Docker deploys its own coordinator/state volume;
inference engine containers contain only model/cache volumes and never share SQLite.

Every run stores its request, chat/project, parent, goal, settings, phases and limits.
One active writer per chat is enforced transactionally. Directory writers serialize;
writing Git children receive distinct worktrees. One priority queue grants a GPU
inference lease, with foreground work before waiting background requests. Network
and file operations in unrelated runs can proceed independently.

Ollama and compatible endpoint streams share a cancellable HTTP transport. A
first-response deadline includes loading and prompt prefill (60 minutes), followed
by a progress-based idle deadline (three minutes) and a 90-minute overall ceiling.
Empty keep-alives do not reset model progress. Stop closes pending reads even
before headers or the first token; metadata probes retain their short timeouts.

Events have a run ID and monotonically increasing sequence. Poll responses expose
`next_cursor` and `has_more`; terminal status becomes `finished` only after pending
events are delivered. Token events are batched to reduce database writes and UI
updates. Clients can reconnect without consuming another client's events.

An invocation journal records arguments before execution and completion before a
model sees the result. Approval hashes bind tool, arguments and target. Running
invocations become outcome-unknown on restart. A user inspects the effect and records
evidence; continuation reconciles protocol results without repeating the action.
Unstarted actions are marked not executed rather than replayed on Resume.

Compaction operates only on completed rounds. The exact current request, settings,
goal checklist and checkpoints remain independently persisted. The ordinary and
summary paths use the same Unicode/image/schema budget estimator. Each round's
compaction episode makes at most one model-summary request. Failed summaries and
further budget reductions use coverage-safe deterministic checkpoints.
Full messages and tool artifacts remain saved. An unfit continuation pauses with a
recovery action. No saved effect is executed by the compaction code.

Goals journal their ordered tasks in SQLite, then atomically replace TODO.md. A hash
detects external edits before overwriting. Goal context is reloaded before each
round and after resume. Parent/descendant counters share the goal allowance, including
compaction requests. Usage stores backend or estimated counters per unique request
ID, UTC timestamps and decode time; timezone conversion occurs during aggregation.

Project tools validate paths, links and expected file hashes. Windows actions use
UIA snapshots, run/focus/window identity guards and bounded coordinate fallback.
The isolated Playwright browser uses snapshot guards; the optional native-messaging
extension connects only selected tabs. Skills and MCP metadata never grant access.
Credentials stay in Windows Credential Manager (or an OS keyring), referenced by
configuration. Raw dictation audio is never persisted.

## Forge 5 execution and Builder

The versioned prompt compiler prepares one bounded context snapshot per model round.
Tool and skill catalogs cache reviewed metadata, expose lazy discovery/retrieval, and
reload scope changes between completed rounds. Schema-validated independent reads
can execute concurrently while invocation results retain deterministic protocol order.

Builder extends existing plans/goals/runs through revisioned briefs, requirements and
fresh evidence gates. Managed command sessions own process trees and logs; preview
sessions bind loopback origins to a separate WebView2 profile and validated pane.
Document inputs and exported artifacts retain scope, provenance and file hashes.

Free cloud specialists remain read-only. Advisory failures are consumed as unavailable;
required child failures remain blockers. Existing strict goal review is preserved,
with explicitly configured local fallback for optional Builder outages. Schema 6 adds
phase timing and token breakdown fields to usage. See [Forge 5](FORGE_5.md).
