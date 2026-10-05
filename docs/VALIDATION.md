# Forge 4.0 validation

This report distinguishes automated contracts, live Windows/model checks and
configurations that still need deployment testing. Measurements use disposable
projects and synthetic prompts. Private chats, local inventory, raw benchmark
sessions and machine paths are not included in the repository or release.

## Automated checks

The complete Python suite passed **425 tests**, with one platform-specific skip
and one upstream Starlette/AnyIO deprecation warning. The frontend passed **15
tests**, strict TypeScript checking and the Vite production build. `npm audit`
reported zero known vulnerabilities for the locked frontend dependencies.

Covered contracts include:

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
- Browser automation was checked with an isolated installed Chromium instance.
  Existing-tab access requires explicitly loading/enabling the extension and
  registering its native host for that browser installation.
- Dictation is implemented with CPU INT8 faster-whisper and in-memory audio.
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
