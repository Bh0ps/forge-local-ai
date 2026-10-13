# Forge 5.1.6 Windows preview

This preview extends saved-engine safeguards to OpenAI-compatible engines and
OpenRouter, and simplifies the connection editor. Forge keeps its warm colors,
composer modes, context controls, temporary helper conversations and project tools.

## A clearer connection editor

Name, preset, editable URL and key stay in the main form. The manual API protocol
selector moves into **Advanced** for presets and opens by default for custom
connections. Changing a protocol preserves focus and clears the unsaved key.
Testing retains its existing permissions and does not choose a new model.

Forge validates the provider journal before creating a replacement credential.
If saving fails, it removes the new key only after confirming unchanged saved
profiles and no reference to that key. Existing keys, completed saves and uncertain
readbacks retain their credentials; cleanup errors preserve the original failure.

The editor uses the existing typography and colors. Desktop, narrow and enlarged
layouts, native disclosure keys, Escape/focus restoration, light/dark contrast and
reduced motion are checked with disposable demonstration data.

## Accepted providers stay attached to their work

Saved Compatible and OpenRouter requests retain their accepted provider through
queues, tool rounds, compaction, supported performance checks and restart. A changed
endpoint, key reference, enablement or privacy scope refuses the next generation
request before dispatch. Completed actions and checkpoints remain available and
are not replayed by Resume. Review the checkpoint and start a fresh request when
the original authority can no longer be established.

Headers are resolved lazily inside the actual client. Forge rechecks the saved
connection after credential lookup and catalog work, immediately before HTTP.
Declared missing or unreadable credentials cannot silently become anonymous
requests. Anonymous engines without a configured credential retain their behavior.

OpenRouter also rechecks current consent, global permissions, enabled helper and
assigned resources before sending a prompt or retained tool evidence. Fresh
zero-price catalog validation, exact serving-model pinning, reasoning-state
preservation and specific quota/authentication/protocol recovery remain in place.
There is no paid fallback or guessed model alias.

Routine catalog refresh and OpenRouter connection-test metadata do not change an
accepted security scope. A small private durable revision records actual security
changes, so changing a scope and restoring its previous values across restart
does not silently restore authority to old work.

The existing `openai` and `compatible` saved protocol labels remain supported
alongside `openai-compatible`. Unknown protocol values are rejected before a
connection or credential is saved; these labels do not imply model capabilities.
Existing remote benchmark eligibility stays unchanged. Remote calibration and
tuning are not enabled by this patch.

## Private state and recovery

The authority journal contains bounded provider identities and revisions. It is
an additive private table with its own format version; workspace schema remains 7.
Existing data is backed up and checked before the table is first created. Private
authority facts remain outside public chats, activity, diagnostics and exports.
Unknown journal formats refuse affected work and preserve its checkpoint.

The delivered 5.1.5 runtime can reopen both empty and populated authority tables.
Recovery guards include added and advanced revisions in their comparisons, retain
later work, and preserve completed actions. No journal is excluded to permit an
unsafe rewind. The prior Ollama binding format remains compatible.

Verified GitHub downloads retain the [publisher and restart checks](FORGE_513.md#downloads-trust-and-recovery).
The Windows executables do not yet have an Authenticode certificate. New runtime
regressions use fake HTTP, a memory vault and disposable state; release notes
record the completed full-suite and packaged Windows validation.
