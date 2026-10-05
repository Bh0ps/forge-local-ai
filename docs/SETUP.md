# First-run setup

The setup wizard scans the computer, connects or installs an engine, helps select
an appropriate model, explains permissions, and verifies the selected workflow.
Construction and opening setup never download models, warm them, record audio, or
change saved preferences. A migrated installation retains its settings and is
identified by the schema-5 `setup_origin: upgraded` marker. A new installation
keeps its first-run state through unfinished steps and restarts. Skipping setup
dismisses the wizard and requests cancellation of an active setup job.

## Durable steps

Scan, guided engine installation, model download, model verification, and browser
verification run as background jobs in the existing entity store. Each job retains
its request, progress, partial results, terminal state, and completion time. A
restart marks queued/running/cancelling jobs interrupted. `setup_retry` starts a
new explicit job using the original captured model, provider, and context; it
does not replace those selections with newly edited defaults. An interrupted
Ollama download can reuse the engine's already downloaded layers when retried.

Cancel persists the cancelling state and signals the active operation. Streaming
model downloads close the waiting HTTP stream promptly; engine installation
checks cancellation between chunks and before startup. A cancelled inference may
leave an already requested engine load finishing in the engine. Completed results
and job availability are committed under the setup lock, avoiding a completed-job
race with the next step. Installation and verification require active runs to be
paused. Setup verification also refuses a conflicting performance job.

Guided Windows installation reads the pinned official Ollama release metadata.
It requires the exact official ZIP asset URL and a published SHA-256 digest before
passing the install to RuntimeManager. It never overwrites an external engine or
its configuration. A successful install returns the managed provider ID and
`requires_selection: true`; the UI explicitly selects it before downloading into
its managed model store. Other engines and advanced profiles use the existing
Models settings and managed-runtime controls.

## Recommendations and evidence

Scan reports installed models and labels capabilities as **engine-advertised**.
The small official Qwen3.5 catalog labels its capabilities as
**publisher-advertised**, with [the model library](https://ollama.com/library/qwen3.5)
as provenance. Recommendations prefer installed models that advertise both tools
and vision, fit a conservative weights estimate, and have a usable advertised
context limit. They report whether current free memory appears adequate. They
require acceptance and never change the selected model or context automatically.
Weights fitting memory does not establish context capacity or response quality.

Verification sends actual requests at the captured selected context: initial and
warm coding checks, a validation tool call, an image containing known text, and a
bounded context-marker recall request. Coding output is validated structurally;
model code is not executed. Tool names/arguments, image words, completed responses,
and truncation status determine pass/fail. Failures and unsupported capabilities
remain visible. Every inference request uses the normal provider queue and usage
ledger. Loading retains the one-hour first-response allowance.

The per-model qualification result identifies provider, model, selected context,
advertised limit, observed prompt tokens, and evidence. It explicitly reports
`filled_context: false`: short and bounded probes do not qualify a filled large
context window. Missing counters are labelled estimated. Verification does not
silently reduce a user's selected context when the engine advertises a smaller
maximum.

## Browser and microphone checks

Browser readiness comes from `service.integrations.browser.native`. The explicit
`setup_browser_verify` button creates a disposable native window/profile and a
generated loopback HTTP page. It navigates and inspects that page, then closes the
window and local server. It does not navigate the user's existing Forge browser
page or fetch an external website.

Model verification checks speech-model readiness and available input devices. It
does not start capture. The user must explicitly start/stop dictation, review the
editable completed transcript, and call `setup_dictation_confirm` with its job ID
and `accepted: true`. Setup stores the model, confirmation time, and character
count as evidence; it does not retain the transcript or audio. A readiness report
alone is never labelled a verified microphone.

The wizard provides Start, Stop and Cancel microphone controls and an editable
test transcript. Confirmation sends only the completed job ID and acceptance,
not the edited transcript. Closing the wizard cancels any microphone operation it
started, including a start request that completes after closing. Browser checking
and microphone capture require their own explicit button presses.

## Service actions

| Action | Contract |
| --- | --- |
| `setup_status` | State, origin, first-run/resume flags, recent durable jobs and catalog |
| `setup_scan` | Starts a read-only hardware/connected-engine scan |
| `setup_install_engine` | Starts pinned official guided Ollama installation; input `engine` |
| `setup_download_model` | Starts a catalog model pull; input `model`, optional explicit `provider_id` |
| `setup_verify` | Starts real checks; optional explicit model/provider/context captured at start |
| `setup_browser_verify` | Starts the disposable local browser check only after this explicit action |
| `setup_cancel` | Signals the job identified by `id` and durably records cancellation |
| `setup_retry` | Starts an interrupted/cancelled/failed job again from its captured request |
| `setup_dictation_confirm` | Confirms an existing completed transcript using `id`, `accepted: true` |
| `setup_complete` | Records the selected provider/model/context; requires a selected model and no active setup job |
| `setup_skip` | Dismisses setup and requests cancellation without changing preferences |

The source tests use isolated profiles, synthetic provider replies, fixture
downloads, and a disposable local browser page. They do not install real models,
record microphones, or modify the user's engines. A clean-PC package test requires
an existing usable Windows Sandbox or VM; hardware availability alone is not such
a test environment.
