# Forge 5.1.5 Windows preview

This revision fixes authenticated saved Ollama engines and keeps their tasks
bound to the saved engine they started with. It preserves the 5.1.4 chat layout, modes,
context colors, presets and temporary-helper behavior.

Saved Ollama keys now reach discovery, model details, chat, residency, engine
version and model-management requests. Forge reads the key when each request is
admitted, then rechecks the saved endpoint and configuration. It does not cache
the key or silently send an anonymous request when a required key is missing.
Connection testing follows the same missing-key rule.

Queued and multi-round saved Ollama requests stop when their engine binding is changed,
removed or disabled. Cancellation and shutdown stop later admission. Completed
tool actions retain their journals and are not replayed by Resume. HTTP failures
after dispatch are not described as known unexecuted actions.

A small private provider-run journal preserves that binding across restart.
It stores bounded identities and a signature, with no tokens or plaintext
endpoint/credential references. It is excluded from public chat, activity and
exports. Existing tasks without sufficient binding evidence keep their checkpoint
and receive a fixed instruction to review it and start a fresh request.

An unreadable or unsupported journal does not count as proof that a binding is
absent. Saved work with uncertain origin retains its checkpoint and requires a
fresh request; a new unrelated compatible request remains usable.

The journal uses an additive private table with its own format version; the
workspace schema remains at 7. Forge backs up existing data before first creating
the table. The 5.1.4 runtime can still open the workspace. Rollback guards keep
new journal rows in their state comparison and preserve later work instead of
silently rewinding it.

Redirects remain unfollowed. Empty non-success responses produce fixed failures
rather than appearing to complete a model-management action. Existing anonymous
local engines retain their behavior.

Runtime regressions use synthetic endpoints and disposable state. No real
credentials, models, inference or hardware are needed for the new authentication
and binding fixtures. Release notes record the completed validation gates.
