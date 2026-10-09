# Forge 5.0

Forge 5 adds an integrated web-app Builder and improves the local agent's execution protocol. The Python coordinator, SQLite history, existing projects, model settings, permission profiles, and free-only OpenRouter connection remain compatible.

## Build an app

Open **Builder**, select a connected project, and save a brief with an objective, audience, design direction, requirements, and acceptance criteria. Existing projects retain their framework. The starter chooser creates files only in an empty app directory: static HTML, React/TypeScript/Vite, or FastAPI/SQLite. Dependency installation is explicit.

**Build with local agent** starts the existing durable goal protocol. The agent inspects files, implements changes, starts managed previews, verifies behavior, and records evidence. Brief revisions preserve requirement identities and reconcile unfinished goal work. The Changes and Checks tabs retain source changes and verification receipts.

Previews run on loopback and have their own process lifetime and browser profile. Pausing generation keeps a preview available. Stop Preview, project removal, or Quit stops owned processes; restart never adopts a saved PID. The desktop uses a separate WebView2 pane. Browser clients use a loopback iframe. Responsive presets, guarded interactions, screenshots, and bounded console/network diagnostics support the repair loop.

Build and functional gates accept completed, successful, appropriately scoped command evidence. Preview readiness does not prove functional correctness. Changing relevant files invalidates passing evidence. Structural document verification does not establish appearance or factual accuracy.

## Local executor and free specialists

The prompt compiler combines a compact operating policy, mode, capabilities, scoped project instructions, active workflow, and current task. Root and applicable nested AGENTS.md instructions load automatically; CLAUDE.md is supported where AGENTS.md is absent. Tool outputs, web pages, attachments, history, and cloud advice remain evidence rather than permission grants.

The agent can discover and load tools as its task changes. Complete calls are schema-checked before execution. Invalid calls receive one repair opportunity; repeated errors require a different approach. Independent read operations may run four at a time. Writes and browser interactions remain ordered and journaled.

Long commands use managed sessions with output offsets and bounded waits. Started actions with uncertain outcomes still require inspection before continuation. Compaction checkpoints cover actual complete rounds, preserve the exact request, and retain retrievable evidence references. Required failed or unconsumed helper results cannot silently become success.

Small contexts use a complete primary workflow plus references to supporting recipes. Briefs and attached notes have bounded reference packets and paged reads; their complete requirements remain stored. Compaction makes one model-summary request per round and repairs any remaining context excess deterministically. Unchanged successful read loops receive a prompt to advance the task.

Explicit selectors, state targets, and API contracts take priority over generic recipe patterns. Failed acceptance checks remain evidence even when their tool invocation completed successfully. The next round receives bounded failure details and evidence references, including after compaction or restart. Repeated unchanged checks require a different repair approach or a concrete blocker. Activity and the run inspector show this distinction; diagnostic tasks can still finish with a factual failure report.

In **Settings → OpenRouter**, configure the connection and existing remote-context consent, then choose **Set up Builder specialists**. Planner, design, coding-advice, diagnosis, research, and review profiles are read-only and free-only. The local agent applies changes. Planning results are associated with a brief revision and project facts; at most two cloud helpers run concurrently.

Optional advisory outages continue locally without quota retries or paid fallback. Builder review outages are explicitly labeled **Local checks only** after local gates pass. Enable **Require cloud review for Builder completion** to pause instead. A valid negative review still requires repairs. Existing non-Builder goal-review behavior remains strict.

## Skills, references and outputs

Library includes 24 original workflows in six bundles. Every recipe states use/avoid conditions, prerequisites, steps, valid tool examples, verification, stopping conditions, and recovery. Routing considers intent, project facts, workflow phase, capabilities, and outcomes. Longer references load on demand.

Use composer selections for a task or searchable agent-profile selectors for recurring workflows. Activity shows skill activation; **Inspect run** shows selected tools, context token estimates, invocation outcomes, and measured request phases. Existing global selections remain supported. `/skill NAME` validates an enabled entry and saves it for the current chat.

The Skill Workbench provides optimistic-concurrency edits, routing previews, resource inspection, history restoration, and bundled-version comparison. Upgrades preserve locally edited/deleted starter files and enabled/automatic switches. Learned proposals remain reviewed rather than automatically activated.

Attach images or TXT/Markdown/PDF/DOCX/CSV/TSV/XLSX references in the composer. Documents are bounded, digest-addressed, scoped inputs; the model must retrieve relevant extracts. Explicitly attached Space notes join that task's references. No document macros or spreadsheet formulas execute during extraction.

Builder can export TXT, Markdown, CSV/TSV, XLSX, DOCX, and PDF. Actual format bytes and hash/structure receipts back the result cards. Download checks the current file hash. The desktop uses an explicit Save dialog; browser clients download the verified bytes.

## Performance and validation

Request accounting separates queue wait, reported model load/prefill/decode, first meaningful output, and total time. Missing provider counters remain unavailable or estimated. Context diagnostics break down instructions, skills, schemas, history, and evidence. Metadata caches reduce repeated discovery and settings probes; SQL filters usage by project/model/calendar period.

Adaptive tuning is off initially. Repeated calibration uses five warm coding samples plus longer prompts and supported tool/image checks. Approve passing profiles before adaptive use and unlock model/context independently. Explicit selections restore their locks. Changed model digests invalidate a calibration. Compatibility probes do not prove app-building quality.

Ollama thinking controls use cached supported values, capabilities, and architecture metadata, so supported model aliases receive an explicit off setting. Unsupported controls are handled before generation without new per-round probes. See [Ollama's thinking-control specification](https://docs.ollama.com/capabilities/thinking).

The evaluation harness contains 20 tasks: twelve app/UI/coding, four broader workflows, and four continuity/recovery cases. It supports three repetitions, isolated project reconstruction, identical verification tools, exact source/model/configuration records, resumable progress, and separate local/hybrid reporting. Live inference is explicit; fixture validation is never labeled model performance.

The local comparison uses a documented transport adapter to normalize the requested thinking-off control equally in both versions while retaining the original baseline source. Native and normalized payload controls are recorded. Theme and landing-page contrast use an isolated installed browser because jsdom does not resolve every CSS variable. Unavailable or unsupported paint verification never counts as a pass; rendered color checks do not establish human visual review.

Engineering targets remain 20% fewer failed tasks, 20% lower median duration on mutually successful tasks, and 25% fewer input tokens without material quality regression. Consult the measured evaluation report before claiming those gains.

## Development and release

Use Python 3.12 and the hash-locked requirements-ci.lock, then npm ci in frontend. Run the backend and frontend suites and npm run build. SQLite schema 6 adds request timing/token diagnostics, with a consistent pre-migration backup; existing records remain readable.

Windows release tooling produces a portable ZIP and installer. Packages are unsigned development builds until the configured publisher-signing workflow is available. Forge 5 installs in its own Forge5 folder; existing Forge4 binaries and personal state are retained. Tests, previews and smoke checks should use disposable FORGE_DATA_DIR folders.

Schema 6 is an upgrade of the shared state directory. Returning to Forge 4 requires restoring the saved pre-schema-6 database while all Forge processes are closed; the older application cannot read schema 6. Preserve the current database separately before restoring a backup so work created in Forge 5 remains recoverable.
