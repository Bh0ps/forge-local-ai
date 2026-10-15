# Forge 5.2.2

Settings opens in a centered dialog over the current workspace. Conversations,
streaming output, drafts and reading position stay mounted underneath. Settings
search, keyboard navigation, nested editors and connection-save warnings remain
available inside the dialog. Closing it returns focus to the originating control.

The five workspace accents now have miniature previews drawn from the same
palette tokens as the interface. Selected navigation, user messages, composer and
pane details use the accent more consistently. Project overrides remain distinct
from the workspace default. Controls use quieter borders, smaller icons and
short transitions, with reduced-motion preferences preserved.

The right pane retains all five tabs in a compact header. Skills are grouped by
their existing bundle metadata, attached skills and notes appear first, and
descriptions expand on demand. Saved aliases, imported skills, disabled or missing
selections, project scope and note limits are preserved. The notification button
has been removed; approvals, questions and recovery controls remain available.

OpenRouter distinguishes authentication failures, refusals, access restrictions,
temporary rate limits and upstream capacity errors using HTTP status and bounded,
allowlisted provider metadata. Stale account snapshots no longer claim current
quota exhaustion. Account metadata is advisory and never blocks inference. Retry
timing and the original sanitized failure category remain visible when incomplete
reasoning prevents safe continuation.

A helper response containing only an acknowledgment or promise gets one persisted
correction request within the existing assignment and budgets. The correction and
its counter are saved together. If it still supplies no usable findings, the
local executor receives a typed result eligible for the existing maximum of
three additional reworded attempts. Completed tools are not replayed. Concrete
blockers remain blocked, and concise answers or reviews with no findings can
still complete normally. Ordered private reasoning state, exact free-model
continuation, consent and access restrictions retain their existing guards.

Strata's pipe adapter consumes its validated end-of-turn token identities before
passing text to the response parser. Thinking and tool markers, ordinary literal
text and tool continuations are preserved. An empty native final answer gets one
saved answer-only correction with tools and thinking disabled for that request;
the user's saved settings are unchanged. A second empty answer pauses with an
accurate failure category. Completed tool actions are retained. Legacy delimiter
artifacts are removed only from the prompt projection of verified older native
turns; stored conversations and newly saved literal replies remain intact.

Native Builder previews keep a small inset inside their scrolling owner. Explicit
opening and resize correction handle fractional top-edge rounding without moving
intentional reading scrolls. Attachment remains bounded to the same pane and
preview identity, with cancellation, hidden-window, HUD, modal and replacement
guards. Waiting never repeats navigation or a tool action, and native containment
is independently checked before positioning.

This revision changes neither the native model directory nor installed model
files. The optional native models and their license notices are described in
[the model guide](FORGE_520.md) and [5.2.1 notes](FORGE_521.md).

The coordinator, HTTP health endpoint and updater share one backend version
constant. A release-metadata regression also checks agreement with the frontend
package and lockfile.
