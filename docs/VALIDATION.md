# Forge 4.1 validation

This report distinguishes automated contracts, live Windows/model checks and
configurations that still need deployment testing. Measurements use disposable
projects and synthetic prompts. Private chats, local inventory, raw benchmark
sessions and machine paths are not included in the repository or release.

## 4.1.1 cold-start correction

The previous Ollama stream's 90-second socket read could abort a larger model
before loading or prompt processing finished. Ollama and compatible streams now
share a cancellable transport with a 60-minute first-response allowance, a
three-minute generation-progress idle limit and a 90-minute overall ceiling.
Warm/release controls use the same first-response allowance. These transport
limits are separate from configured goal budgets, which remain unchanged.

A real local HTTP socket delivered its first response after 95.25 seconds and
completed without retrying. Time-scaled regressions cover loading, empty
keep-alives, reasoning/tool progress, idle/total limits, cancellation before
headers/tokens, exact usage and SSE/Unicode boundaries. A paused run resumed
without repeating its completed file write. This validates waiting and recovery;
it is not an additional model-speed measurement.

## Automated checks

The complete 4.1.1 Python suite passed **550 tests**, with one platform-specific skip
and one upstream Starlette/AnyIO deprecation warning. The frontend passed **35
tests**, strict TypeScript checking and the Vite production build. `npm audit`
reported zero known vulnerabilities for the locked frontend dependencies.

Covered contracts include:

- Saved `/plan` → `/todo` execution, reviewed Build deduplication, legacy plan
  recovery, three truncated plan fragments across Resume and immutable goal data.
- Chat/project mutation races, archive/restore, deletion preserving folders and
  usage, schema-4 backup and moved-project continuation guards.
- Real byte/GiB telemetry, capability-gated residency controls, benchmark accounting
  and prompt cancellation, including missing and nullable engine counters.
- HF pinned downloads, checksums, safe paths, cancel/retry, import-name races,
  import reconciliation and matching model/projector checks.
- Dictation capture cancellation races, microphone start failure and the tested
  faster-whisper/PyAV compatibility pin.

- Current-request retention, Unicode budgeting, large tool output artifacts,
  failed-summary fallback, cancellation, truncated model streams and output limits.
- A multi-step goal across several compactions and a coordinator restart, retaining
  task order, evidence and the next action with no duplicate completed writes.
- Atomic tool-result/checkpoint commits, unknown-outcome inspection and more than
  1,100 replayed events across cursor pages.
- All permission profiles, live revocation, OS-derived app scopes and changed
  desktop targets after approval. Project grants cannot authorize unrelated apps.
- MCP stdio, Streamable HTTP and legacy SSE against live protocol fixtures,
  reconnects, namespaced tools, rich results, reviewed plugin imports and skills.
- Windows Credential Manager round trips; no plaintext credential fallback.
- Consistent migration backup, stable project IDs, external project paths,
  conversations, attachment references and file-backup manifests.
- Independent Git worktrees, dirty parent preservation, visible integration
  conflicts, non-Git writer serialization and schedule overlap/catch-up handling.
- Day/month aggregation in local time, timezone transitions, cached-token counts,
  estimated counters, zero decode duration, persistence and request deduplication.

## Windows and interface checks

The compiled 4.1 frontend passed eight live coordinator workflow groups without
JavaScript errors. These exercised context presets, telemetry, native/HF setup,
right-click chat actions, move/archive/restore, saved-plan execution, Build by run
ID, exact TPS and a 620×188 HUD preserving its draft.

The frozen 4.1 WebView2 app passed full/HUD/tray/Quit plus native browser navigation,
inspection, typing, clicking and PNG capture. Website pages have no Forge Python
bridge. Real CPU INT8 dictation transcribed a synthetic in-memory speech fixture;
the microphone format was checked without recording ambient audio.

The real WebView2 host was exercised from source and a frozen portable build.
Checks included initial navigation, workspace/HUD switching, bounds restoration,
reply expansion, pinning, closing to the tray, reopening and Quit termination.
The source-host smoke completed in about 3.4 seconds; a disposable browser client
became ready in about 1.04 seconds. These are individual observations, not a
cross-machine benchmark or a guarantee of startup time.

All primary navigation pages rendered without JavaScript errors in a disposable
workspace. Local attachments retain filesystem references and hydrate on demand;
stream updates are batched and active history is bounded. Keyboard controls,
system themes, compact model selection and recovery/approval controls are covered
by the frontend tests and manual smoke checks.

## Live local inference

The 4.1 upgrade completed a real 9B-class model `/plan` followed by `/todo`: the
plan made no file changes; the goal completed 15 rounds and 14 tool actions,
including file creation, Python verification and saved checklist evidence. At
16K context with reasoning disabled, warm short coding probes reported about
85–87 output tokens/s; the streamed harmless tool probe passed at about 83
tokens/s. A large-text vision fixture returned the exact expected text. Smaller
text was misread, so recognition quality remains model-dependent. Short probes
do not validate a filled 16K or 256K window.

A public 1.19 MB GGUF was downloaded at an immutable revision, checksum-verified,
imported through Ollama's blob/create API and discovered with its actual completion
capability. Its generation was incompatible with the external engine's q8 cache
block size; Forge did not change that engine or promote the toy model. All synthetic
test models were removed. Download/import success is distinct from inference
compatibility.

The following are retained observations from the 4.0 baseline:

Identical short coding prompts and settings were sent through Forge to the same
installed 9B-class quantized model, with an 8K context and reasoning disabled:

| Observation | Cold request | Warm request |
| --- | ---: | ---: |
| Complete request latency | 85.87 s | 1.04 s |
| First visible token | 83.64 s | 0.36 s |

The cold result was dominated by loading/prefill. Warm retention materially
reduced latency in this sample; it does not eliminate cold loading or image
prefill. A file-tool task read the expected synthetic marker and finished in
1.47 seconds. Five recorded requests reported 4,749 input tokens, 208 output
tokens and 2,061 cached input tokens, with weighted decode speed of 64.22 tokens/s.
These counts include the initial vision probe described below.

A single-line vision fixture was initially misread. Follow-up multiline fixtures
were read correctly by both tested quantizations, and an end-to-end Forge vision
request correctly returned `FORGE`, `VISION`, `482`. That request took 31.48
seconds including cold image prefill; its reported decode rate was 68.20 tokens/s.
Vision is functional, but model recognition quality remains a material limit.

No external Ollama process settings were changed for these measurements. There is
no controlled comparison to another application, and Forge makes no superiority
claim. Peak-memory instrumentation is available in managed-runtime validation;
no aggregate peak-memory or cold-start improvement claim is published here.

## Deployment and advanced-profile limits

- Docker is unavailable on the validation machine. Compose YAML and pinned image
  digests were checked, but container builds, GPU passthrough and the vLLM/SGLang
  profiles were **not executed**. They require testing on the deployment host.
- MCP OAuth flow was tested against fixtures. Signing into each external vendor
  is a separate setup and interoperability check.
- Browser automation was checked with native WebView2 and an isolated Chromium instance.
  Existing-tab access requires explicitly loading/enabling the extension and
  registering its native host for that browser installation.
- Dictation is validated with CPU INT8 faster-whisper and in-memory audio.
  A transcription model must be installed explicitly; microphone recognition
  quality depends on the device, language and model.
- Quantized KV cache, speculation, MTP, n-grams, CUDA graph tuning and native NVFP4
  remain gated by supported executable flags, compatible models/hardware and the
  exact validation binding. **No advanced profile was promoted from feature
  availability alone.** Run coding, image and streamed-tool probes against the
  baseline before accepting a measured promotion.
- Permissions are enforced by the coordinator; commands still run with the
  Windows user's OS permissions. This is not a virtual-machine sandbox.

## Reproduction and release hygiene

Use the commands in [Contributing](../CONTRIBUTING.md) with a fresh
`FORGE_DATA_DIR` and synthetic projects. The build includes dependency notices,
an installer and a portable directory; retain `_internal` beside `Forge.exe`.
Release source contains no application database, chats, model binaries or
benchmark-session logs. Commit identity is neutral and MIT is preserved.
Keep the previous Sidekick installation and its original data for rollback.

Engine integration follows primary documentation:
[Ollama configuration](https://docs.ollama.com/faq),
[Ollama timing fields](https://docs.ollama.com/api/chat),
[llama.cpp speculation](https://github.com/ggml-org/llama.cpp/blob/master/docs/speculative.md)
and [vLLM consumer Blackwell support](https://docs.vllm.ai/en/latest/features/quantization/b12x/).
These references describe available mechanisms; the measurements above determine
what this release actually verified.

## Forge 4.2 release checks

The October 5, 2026 upgrade passed 813 Python tests and 65 frontend tests in a
separate environment installed from the hashed dependency locks. Subsequent
focused updater checks include interrupted-install recovery: unknown installer
outcomes block another automatic attempt. Source TypeScript compilation and the
production frontend build passed.

Disposable native Windows checks covered fresh first-run setup, workspace/HUD
transitions, tray operation, closed-window API access and the embedded WebView2
browser. Agent navigation, inspection, typing, clicking and screenshot capture
used the same native page; websites had no Forge JavaScript bridge. Synthetic
native-messaging fixtures covered separate Chrome/Edge tab identities and stale
or cancelled actions. Browser controls and critical HUD controls fit at 125% scale.

Production UI fixtures exercised memory review, Telegram setup/pairing, GitHub
scope and clone confirmation, OpenRouter consent/profile creation, notifications
and signed-update gates with no browser console errors. Protocol tests cover
atomic run completion, permission changes on accepted connected tasks, cancelled
chat delivery, external-event deduplication, rollback quarantine and malformed
usage counters. A buffered output burst cannot inflate the backend speed estimate.

Live public GitHub browsing, README retrieval and a disposable managed clone
passed. Live public OpenRouter metadata returned the free router and advertised
tool/vision capabilities after correcting optional pricing-field handling.
Authenticated cloud generation, real Telegram deliveries and remote PR creation
were tested with fixtures; they require account configuration for live acceptance.
No credentials or personal profiles were used in these live public read checks.

Signing setup is deferred. The Windows packages are unsigned and automatic
installation is fail-closed until a trusted publisher is embedded. Clean-VM and
Docker/GPU acceptance remain unexecuted on this machine. There is no new
hardware throughput or superiority claim from these interface and protocol fixes.
