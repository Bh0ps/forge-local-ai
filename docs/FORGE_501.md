# Forge 5.0.1

New tracked goals use persistent work packets: inspect, implement, verify, repair,
then report. Each packet retains the exact task identity, accepted scope revision,
paths, unresolved checks and any current cloud guidance. Existing saved runs keep
their execution mode and invocation journals. The coordinator derives verified
progress from registered, complete, fresh receipts; a file or an answer alone is
not proof that a requirement passed.

Tasks without a registered checker remain candidates until a current structured
independent review or explicit human review establishes their coverage. Human
review is a deliberate action in task details, requires a note and the current
goal revision, and cannot override a failed necessary check. Paused guided goals
remain visible so unsupported checks and interruptions can be resolved. Legacy
saved runs retain their original completion behavior.

Guided compaction preserves the newest complete tool exchange for the next model
request. Older covered history can be checkpointed; oversized fresh results pause
with a context-limit message and remain available on Resume. Compact packets avoid
duplicate failure details and explicitly request full goal retrieval when task
text is only partially displayed.
Registered schema metadata remains available to coordinator preparation during
compaction; only the model payload and its token budget use public wire schemas.

Successful, journaled file mutations supply bounded diff excerpts in model context.
Their full arguments, result artifacts, hashes and backups remain intact and
retrievable. Reads, verification results and uncertain mutations keep their full
publication semantics; an excerpt never authorizes replaying an action.

Current registered checks can defer duplicated contrast calculation details to
their original artifact. Every check object, actionable failure, primary fact,
cleanup result and source identity remains complete. Duplicate failure details
are omitted from the coordinator packet only when the exact checks are present
in the same context snapshot. Ownership, permissions, registration, artifact
contents and source freshness must match; uncertain results remain unchanged.

For guided Ollama goals with an available registered checker, three committed file
mutation calls make verification due. Another file mutation remains unexecuted
until a real check runs; a genuine failed check opens the repair allowance. The
journal-backed count survives restart and checkbox updates. Accepted scope changes
or applied steering start a new allowance. Goals without an available checker,
legacy runs, commands and browser interactions retain their existing behavior.

Once current typed evidence and coordinator gates establish readiness, the final
report turn offers no task tools. Readiness is checked again after generation;
stale evidence or new steering returns to verification. A model that still emits
tool calls receives saved unexecuted outcomes and a concise report grounded in
current receipts. Required independent review still runs before final completion.

Targeted task updates carry the task ID and expected goal revision. Legacy progress
updates retain supplied IDs or unique exact task text, and reject ambiguous
replacement. Accepted requirement changes advance the scope revision; ordinary
progress does not request a new cloud plan. Registered checks retain their trusted
scope even when a failed check returns a shorter list of labels. Source fingerprints
and permission checks prevent old or unrelated results from clearing a blocker.

## Everyday workspace

Draft text and valid document, skill and Space references are stored locally by
chat/project or Builder scope. Autosave uses a 500 ms debounce, flushes on navigation
and checks the saved revision. A conflicting or delayed save preserves the current
text. Discard explicitly clears the saved draft. Unsent images stay in session
memory. Each asynchronous submission retains its originating scope and identity;
uncertain outcomes are inspected instead of repeating side effects.

Task status and recovery are visible beside the composer. Detailed context, tools,
skills, evidence and measured preparation timings remain in the run inspector.
Attachment processing and failures are visible before submission. Builder keeps
editable drafts separate from saved briefs and runtime checks. Unchanged Build
retains its brief revision, while changed content uses Save & Build. Preview errors
and logs remain available; browser clients can reload their preview frame.

Browser pairing requires the session cookie and a separate client proof bound to
the exact coordinator origin. A preview server receiving a host-wide cookie cannot
use it to invoke Forge. The browser stores the proof only for that origin; native
bridge calls retain their existing path. Existing cookie-only browser sessions must
pair again. Unpair revokes its captured session without erasing a newer pairing.

## OpenRouter guidance

OpenRouter settings expose **Use OpenRouter guidance for goals** and explicit AI
workflow setup. The existing OS-vault key is reused. Account configuration,
metadata authentication, free capacity, specialist readiness and live workflow
verification are separate states with timestamps. Metadata authentication alone
does not mark the workflow verified.

A substantial ordinary tracked goal or accepted-plan build requests one plan for
its accepted scope revision, including goals without a project. Inspection can
continue while planning is pending. Implementation waits for current advice or
the original 60-second advisory deadline; a missing optional plan then produces a
visible local-continuation status. Advice is pinned in the local context before
an implementation action and cannot alter requirements or permissions.

**Request new guidance** records a revision-checked planning request for a stopped
goal. Resume then runs the planner. Its journal prevents duplicate requests after
an uncertain response; it retains the accepted requirements and earlier advice.

All cloud assignments are read-only and share a two-assignment limit, including
reviewers. Private memory, unrelated history, automatic project guidance and
protected configuration are excluded. Files, artifacts and attachments must be
allowlisted for the assignment and permitted when requested. Free-only price
checks, no paid fallback and no automatic quota/authentication/capacity retries
remain enforced. Actual returned model IDs are retained separately from the free
router alias. Required independent review remains a completion gate.

The same artifact boundary applies before preparing review prompts, selecting
specialist evidence and returning tool context. Protected or unpermitted resources
and opaque command output remain local. Reviewers receive typed command status and
safe scoped evidence instead. When known protected sources or supplied private
memory contributed to a local run, generated candidate narratives remain local
too. Memory provenance stores counts and digests, without recalled text, and stays
sticky through compaction and restart; local memory preferences are preserved.
This boundary uses resource identities and known narrative provenance. Explicitly
assigned safe project files and code/check artifacts remain reviewable; it does
not claim to track every possible transformation of private text. Patch, move and
restore results contribute their committed file paths to review freshness hashes.

Excluded-answer notices cannot be cited as implementation evidence, including in
restored review results. Local review snapshots retain known protected file hashes
for freshness without putting them in cloud context. Private work needs current
task-linked registered checks or explicit local human coverage before cloud review
can certify that task; required independent review remains a separate gate.

**Verify AI workflows** runs journaled disposable ordinary and Builder fixtures.
Its receipt checks planner response, advice actually supplied to the local model,
local effects and checks, independent structured review and specialist results.
Failed or unavailable capabilities remain explicitly unverified. The fixture is
a connection test, not a general quality or speed benchmark.
Its independent CSV check uses a tested existing Python interpreter, skipping
Windows Store aliases. Discovery changes no settings or PATH and installs nothing.

## Compatibility and validation

Versioned `VerificationReceiptV2`, `WorkPacketV1`, `WorkflowStateV1` and progress
summaries use additive records. `goal_task_update`, scoped `draft_get/save/clear`,
`builder_runtime_get`, `ai_workflow_status/setup/self_test` and `submission_get`
extend the existing dispatcher. Existing tools and slash commands remain usable.
`goal_task_accept` records direct human review and is never exposed as a model
tool. Cloud consent, current permissions and specialist enablement are checked
again after the inference queue grants a request its lane.

Update readiness distinguishes active model operations from retained job history.
The helper validates both Forge 4 and Forge 5 installation targets and the retained
publisher identity. Rollback keeps a consistent current snapshot and refuses to
rewind newer user work or action journals; inspect the retained state before any
manual restoration.

The 20% failure, 20% mutually successful median-time and 25% input-token targets
are measured against fixed local settings. New-model and hybrid runs are reported
separately. Release validation must retain raw runs, hashes, applied continuity
interventions and category results; passing unit tests does not establish gains.
