# Forge 5.1.0 Windows preview

This unsigned prerelease improves helper reliability and daily chat while retaining
local models, project tools, Builder, browser previews and permission controls.
The [v5.1.0 release](https://github.com/Bh0ps/forge-local-ai/releases/tag/v5.1.0) includes
a Windows installer, portable ZIP and SHA-256 checksums.

## OpenRouter helpers

The free router chooses the first model for an assignment. Forge verifies that
the exact returned model is advertised as free and meets the selected context
and tool requirements before continuing. It does not guess a free variant or
change saved profiles. Opaque reasoning state is stored separately from public
messages and returned only to its bound provider/model.

Incomplete signed exchanges are also retained privately. A disconnect,
interruption, malformed call or output limit cannot authorize replaying an
unfinished exchange. Forge pauses before sending another round or executing a
partial tool call. Inspect the saved progress and start a new user request;
an unrelated helper does not resolve a required assignment's blocker. Explicit
model refusals retain their refusal category and can use the fresh retries below.
This follows OpenRouter's [reasoning preservation requirements](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens#preserving-reasoning).
State already discarded by an older version cannot be reconstructed; those
failed exchanges may require a new scoped request after upgrading.

An explicit model refusal or empty result is a failed attempt, not successful
findings. The local executor receives a typed result and instructions to reword
the same assignment. `delegate_agent` accepts `retry_of` to retain the logical
assignment, original scope and completed evidence. Up to three extra attempts
are allowed within the existing shared task limits. Use `required=false` for
optional advice; required work remains a completion blocker. If the helper
profile changes provider, Forge rejects the retry before creating another run.

Quota, authentication, consent and protocol failures keep their specific recovery
paths. A denied resource call can be consumed as a safe refusal, but cannot
grant access. Independent goal review retains its strict evidence requirements.

Expand task activity to inspect helpers and their actual serving model. The run
inspector exports a small JSON diagnostic report without prompts, conversation
text, paths, credentials, raw remote errors or opaque provider state.

## Your workspace

Chat uses a centered reading column and a quiet composer. Task activity groups
tool invocations by durable identity; saved activity remains available after
reopening a chat. Loading older messages preserves the reading anchor, and
Jump to latest returns to new output.

The sidebar puts New chat, search and the project/chat tree first. Builder,
Spaces, Scheduled, Library, Agents, Usage and Memory remain in Tools.

Settings includes a workspace name, Copper/Sage/Blue/Violet/Rose accents and
motion preference. Project actions offer a separate icon and optional accent.
Model menus identify known reasoning support; dynamic routers show that
reasoning varies by route, and unknown metadata is not labeled unsupported.
Resize panes with their edge handles or use arrow keys on a focused separator.
Pane widths, visibility, selected tab and collapsed projects are saved by the
coordinator for the desktop client, independent of the changing local port.

Welcome and active-work accents use restrained opacity motion. Hidden windows
and reduced-motion preferences stop animation. Streaming characters are not
decoratively animated.

## Verification and recovery

Scheduled occurrences use a stable UTC admission identity. Restart reconciles
an admitted run instead of replaying it; legacy uncertain occurrences are
recorded and future scheduling advances. Completed/cancelled work is not reopened
by Pause. Telegram sends eligible replies before applying the batch limit while
preserving chunk order.

Builder checks save source hashes before launch, after exit and at evidence
recording. Exact generated JUnit, coverage and TypeScript cache files can be
classified as check outputs. Tracked source files and conflicting existing
source files remain part of verification. Older command sessions need a fresh
check to establish an automatic source snapshot.

Research and preview browsers ignore stale navigation completions and guard pending Back/Forward/Reload actions, so an older page load cannot make a new request appear ready.

The Forge-authored dependency attribution checksum matches a clean LF source
checkout. Upstream license text is unchanged.

Schema 7 adds private provider-turn state. Upgrading an existing database creates
a consistent `pre-schema-7` backup before migration. Close Forge before restoring
that backup; older applications cannot open a migrated workspace.

## Validation

Validation on Windows, 12–13 October 2026:

- Backend cumulative coverage: 1,818 passed, one skipped across 1,819 tests.
  Prior coverage had 1,813 passes and one skip across 1,814 tests; the final
  native/browser/integration check passed 73 cases, including five additional
  navigation/history regressions. Earlier focused checks passed all 61
  continuation/delegation cases and all 22 license-collection cases.
  One legacy compaction fixture exceeded its eight-second deadline under parallel
  load and passed unchanged in 3.43 seconds when rerun serially.
- Frontend: 204 tests passed across 20 files; TypeScript and production Vite build passed.
- Packaged Windows: workspace navigation, Library, HUD, background close,
  research browser isolation and isolated Builder/standalone previews passed.
- Live OpenRouter: a synthetic fixture completed two rounds and one tool call,
  pinning `dots-studio/dots-3-note-preview:free` served by AtlasCloud. Only
  synthetic task content was used.
- Rendered UI: all five palettes passed checked text/button contrasts in both
  themes; eight layouts from 390px to 1440px and an equivalent 200% zoom passed.
  Reduced motion disabled animation; hiding the page paused ambient motion.
- Native motion: the real Windows host paused the welcome animation on tray
  hide and minimize, and resumed it on restore, even though WebView2 continued
  to report the document as visible.
- Compiled Python: 60 bundled modules/scripts matched the final source.

The synthetic live check verifies tool continuation for the tested route;
model quality and future free-provider availability can vary.
The existing AnyIO deprecation warning and frontend bundle-size warning remain.

The packages are an unsigned development preview. Automatic installation remains
disabled until a trusted signing publisher is configured. Validation used
disposable workspaces; it did not require personal conversations or projects.
