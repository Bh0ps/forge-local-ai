# Forge 5.2: owned local inference

Forge runs GGUF models through bundled workers. A separately installed Ollama,
LM Studio or Strata application is optional. The Python coordinator continues to
own conversation history, permissions, agent tools, helper assignments and recovery.

## Models

Open **Settings → Models & engines → Forge models**. Choose a directory with enough
space, then import GGUF files, import an existing Ollama model, or explicitly download
the curated Strata Coder model. File and directory choosers are available on Windows.
Select **Use** to choose a model. The worker starts when the next request needs it.

Imports check complete SHA-256 hashes and GGUF metadata. Imports on the same volume
use hard links; other volumes receive verified copies. Ollama originals are retained.
Matching vision projectors become Forge-owned files too. Custom Ollama system text,
stop sequences, parameters, template text and licenses remain in the private catalog.
The native runtime uses the GGUF chat template; custom Ollama Go templates are archived
for review rather than interpreted as Jinja. Confirm customized behavior before removing
an original. External adapters are retained for manual review.

The directory stays separate from the application installation, so app updates do not
replace weights. A GGUF saved among Ollama manifests is discovered as a GGUF file without
trying to read its entire contents as JSON. No model weights or personal catalogs ship
in the source repository or installer.

## Engines and resources

llama.cpp handles general supported GGUF architectures. Strata is specialized for the
pinned Qwen3.8 Flash Next Coder IQ1_M model. The initial Windows package includes NVIDIA
CUDA acceleration, with llama.cpp CPU execution available without a compatible GPU.
Strata requires a compatible NVIDIA GPU; AMD Strata binaries are not included in this
preview. The Coder download is 58,408,584,928 bytes, plus generated preparation files. Memory-saving expert preparation can add about
25.2 GB (23.42 GiB) of local files; this is not another network download.

The exact engines, revisions and binary hashes are recorded in `native-runtime.lock.json`.
Selected MIT-licensed Strata artifact, tokenizer and protocol support lives under
`forge_vendor/strata`, with upstream notices and modification provenance. Its separate
web UI, HTTP server and MCP tool executor are not used. Native notices accompany the
runtime files. The two workers share compatible CUDA 13.4 redistributables within the
CUDA 13 major family; Forge does not install system CUDA or graphics drivers.

Workers run hidden, belong to Forge's Windows process job and close with the coordinator.
llama.cpp binds an ephemeral loopback port with a random session key in a private file.
Strata uses a private stdin/stdout token protocol. App/tool output is not written into
native worker logs. Completed tool actions remain in Forge's normal invocation journal.
A worker failure interrupts inference and never automatically replays a tool action.

Local inference shares the existing GPU queue. Another local engine's active task blocks
native loading, and starting an external local engine unloads Forge's worker. Context,
reasoning, CPU thread and keep-warm settings remain explicit. llama.cpp automatically
fits GPU layers and selects its supported attention path. Strata automatically sizes
its expert cache and prompt processing. A memory check chooses a full arena, a resident CPU remainder, or SSD-mapped experts
using Strata’s supported modes, and blocks loads without minimum headroom.
Native IQ packs use Strata's required two-token verification windows. An additional
MTP draft model and larger draft windows are not included. Upstream speed charts do
not establish performance on another computer.

The existing Performance page accepts immutable native model and runtime identities for
measured thread profiles. Recommendations remain scoped to the selected model, context,
hardware and completed probes. Loading, releasing and idle expiry affect owned workers.
No speed gain is claimed without a measurement on the actual model and configuration.

## Verification and recovery

**Check** beside a model runs synthetic text, tool-argument and tool-continuation checks.
It does not use project data or execute project tools. Passing those checks does not prove
vision support, every task, or a full context window. Existing Performance and guided
workflow checks provide additional evidence for those capabilities.

Downloads use fixed revisions and exact byte ranges, retain a resumable partial and
verify the complete SHA-256 digest before registration. **Pause** preserves downloaded
bytes. Interrupted model jobs remain visible after a restart and can be retried.
App updates wait for model operations. The global stop cancels native inference and
model jobs as well as ordinary agent work.

The model picker shows model names while preserving stable request identities. The
Models panel refreshes after completed imports, retries unavailable metadata and ignores
responses after it closes. Existing Ollama and compatible connections remain available
until their users choose to migrate them.
Historical Ollama chat model names resolve to their imported native identities when
available. Missing models leave the current composer selection intact; chat history
and saved run bindings are preserved. Loading earlier messages does not change models.

Source development can acquire the same pinned bundle with
`python scripts/fetch_native_runtimes.py --destination native-runtimes --cache build/native-downloads`.
Use a fresh destination after changing the runtime lock; mismatched existing files are retained, never silently reused.

Windows release validation also loads a pinned public 0.6B GGUF through the frozen
application on disposable state, using CPU inference for reproducible CI. This verifies
native text, tool arguments, continuation, tokenizer/template imports and worker cleanup.
The test weights are downloaded outside the package and are not shipped in Forge.
