# Integrations and optional Docker engines

Forge keeps credentials in Windows Credential Manager, or an OS keyring on other
desktop platforms. Configuration stores opaque references. A headless Linux
container needs a configured OS keyring for authenticated third-party providers;
the default local Ollama connection does not require one. There is no plaintext
credential fallback.

## MCP

In **Settings → MCP**, add a server URL, choose HTTP, and save a disabled draft.
Choose **Test**, then enable the desired tools. Stdio servers use an executable
and an argument array; Forge launches the executable directly without a shell.
Legacy SSE endpoints remain supported.

For a bearer-protected endpoint, enter its token in Authentication. For OAuth,
choose **Authenticate**, open the returned authorization URL, and complete the
provider's normal sign-in page. The official MCP SDK handles OAuth discovery,
PKCE, state checks and token refresh. Forge receives the response through a
temporary loopback callback. The sign-in process is performed by the user.

Tool names include a server namespace and a hash of the original name, preventing
collisions. Server descriptions and annotations are untrusted metadata and cannot
grant permissions. Every MCP call passes through Forge's permission registry.
Unknown operations request approval under Always Ask. Full rich results are saved
in local artifacts; text previews are bounded and images/audio retain binary
artifacts. Interrupted or disconnected calls can have an unknown outcome and must
be inspected before repeating a side effect.

The integration dependencies are pinned in `requirements-integrations.txt`.
Protocol validation includes real stdio, Streamable HTTP and SSE servers, plus a
2024-11-05 protocol compatibility fixture. SDK documentation is available from
the [official MCP Python SDK](https://py.sdk.modelcontextprotocol.io/).

## Skills and plugins

Skills are `SKILL.md` packages with optional YAML frontmatter containing `name`
and `description`. Install a local folder, ZIP or GitHub repository through the
Skills page. Project discovery supports `.forge/skills`, `.agents/skills`,
`.codex/skills` and `.claude/skills`; global packages live under `.forge/skills`.
Explicit invocation and automatic selection are separate settings. Every selected
skill retains its source label and cannot change the permission profile.

Plugin imports support portable bundles, `.codex-plugin/plugin.json` and
`.claude-plugin/plugin.json`, including portable skills and MCP definitions.
**Inspect** stages an inactive private copy and reports its compatibility,
licenses, hooks and host-specific components. **Install** imports that exact
reviewed copy. Server definitions begin disabled. Hooks and setup commands are
never run automatically. Host-specific apps/connectors require a Forge adapter.
Plugin updates are separate reviewed imports; the previous package stays intact.

The reviewed starter catalog provides Research and Code review skills. External
catalogs can be local JSON or HTTPS documents with a `plugins` or `entries` list.
Claude-style GitHub source records are recognized. External entries remain marked
unreviewed. Licenses and notices stay with imported packages. ZIP extraction
rejects traversal, links, reserved Windows paths and oversized packages.

## Browsers

Forge desktop provides a built-in WebView2 research browser with a navigation
bar, Back/Forward/Reload controls and its own browsing profile. It uses the Windows
web runtime and does not require a separate Chromium download. Open it from
Browser settings; approved agent browser tools use it by default on the desktop.
Remote pages do not receive Forge's application bridge. Window/session, navigation
and snapshot checks reject stale actions before clicking or typing.

The optional isolated browser uses Playwright and a separate persistent profile.
Select the isolated backend when needed. Use **Install browser** if Chromium is
missing. The background download
reports progress and places its runtime beneath `.forge/runtimes/playwright`.
Neither browser reads the user's Chrome/Edge profile, cookies or passwords.

Browser inspections return a snapshot ID and target selectors. Click/type calls
must cite that snapshot; changed URLs and changed targets require reinspection.
Screenshots become local artifacts. Browser operations retain the same permission
checks as other computer tools.

For existing Chrome/Edge tabs, install the optional extension and follow
[`browser-extension/README.md`](../browser-extension/README.md). Click its button
to connect a selected tab. Cross-origin navigation drops the grant and requires a
new click. Native messaging authenticates a loopback bridge and registers only the
specified extension ID. Windows native host setup changes current-user registry
entries through an explicit settings action.

## Hugging Face models

In **Settings → Models → Hugging Face**, search public GGUF repositories or enter
an `owner/repository` identifier. Forge shows repository licenses, gated access,
quantization labels, file sizes and projector files before download. Every selected
revision resolves to an immutable commit SHA through the official
[Hugging Face Hub SDK](https://huggingface.co/docs/huggingface_hub/package_reference/hf_api).
Public models need no account. For gated/private models, accept their terms on
Hugging Face and enter a read token; **Clear token** removes its OS-vault reference.
Forge does not reuse an unrelated global Hugging Face login or write plaintext
tokens to its configuration.

Downloads run in the background beneath
`.forge/models/huggingface/<owner>--<repository>/<commit>/`. Jobs show byte progress,
verification and errors. Selected GGUF files retain their original filenames;
available small license/readme notices are preserved alongside them. File sizes
and SHA256 checksums are verified against the inspected Hub metadata. Cancellation
retains partial SDK downloads for an explicit Retry. After restart, unfinished
jobs show Interrupted rather than pretending a model is installed. Forge never
loads repository Python code, converts Safetensors automatically, or enables
`trust_remote_code`.

Choose **Import to Ollama** after the download completes. Enter a new model name
and select the target Ollama provider. Forge validates GGUF metadata, streams
verified blobs through the [Ollama API](https://github.com/ollama/ollama/blob/main/docs/api.md),
then creates the model and refreshes the actual engine model list. It does not
overwrite an existing name or modify Ollama's manifest folders. Split models need
all shards. Vision repositories require a matching downloaded projector or an
explicit text-only choice; recognized projector metadata and available embedding
dimensions are checked. The tested projector import path requires Ollama 0.35.1
or newer. Unsupported combinations leave the downloaded files available for an
explicit llama.cpp model/projector configuration.

Capabilities after import are reported by the engine. A successful import does
not establish model quality, tool correctness, vision quality or compatibility
with every cache profile. Test a prompt/image/tool round before promoting a
performance profile. For example, a tiny model with an 8-element attention head
cannot use q8 KV blocks of 32; Forge does not silently reconfigure an external
engine to work around that mismatch. If an import is interrupted after creation
starts, **Inspect outcome** checks the target model list before Retry becomes
available. It never automatically repeats an uncertain model creation.

## Docker coordinator

Docker is an optional standalone Forge coordinator, with its own named state
volume. Do not bind the Windows `.forge` directory or SQLite database into it.
Inference engines have only their model/cache volumes; they never share Forge's
database. Windows accessibility, microphone capture and selected-tab native
messaging belong to the Windows host. Container isolated browser automation runs
headlessly. A browser client uses the coordinator APIs and does not open SQLite.

Build and start the CPU-compatible baseline:

```sh
mkdir -p workspace
docker compose up --build -d forge ollama
docker compose exec ollama ollama pull qwen3.5:9b
docker compose logs forge
```

Open `http://localhost:8081`. Enter the one-use pairing code printed by the Forge
container at startup; it expires in three minutes. If it has expired, restart the
Forge container to issue a fresh code. Paired sessions last 24 hours. The API does
not accept a `FORGE_API_TOKEN` environment variable; authentication uses pairing.
The application port and inference ports are published on loopback only.

Set `FORGE_WORKSPACE` to an existing project directory before starting Compose to
use that folder instead of `./workspace`. On Linux, the bind directory must be
writable by container UID 10001. The default Ollama container has CPU access. Add
GPU access when Docker and the NVIDIA Container Toolkit are configured:

```sh
docker compose -f compose.yaml -f compose.gpu.yaml up --build -d forge ollama
```

Optional engines can also serve the native Windows app without running the Docker
Forge coordinator:

```sh
docker compose --profile vllm up -d vllm
docker compose --profile sglang up -d sglang
docker compose --profile llama-cuda up -d llama-cuda
```

In Models → Providers, add the matching OpenAI-compatible URL:

| Engine | Windows host URL | Docker coordinator URL |
| --- | --- | --- |
| vLLM | `http://127.0.0.1:8301/v1` | `http://vllm:8000/v1` |
| SGLang | `http://127.0.0.1:8302/v1` | `http://sglang:30000/v1` |
| llama.cpp CUDA | `http://127.0.0.1:8303/v1` | `http://llama-cuda:8080/v1` |

Set `FORGE_HF_MODEL` for vLLM/SGLang, or `FORGE_GGUF_DIRECTORY` and
`FORGE_GGUF_FILE` for llama.cpp. `FORGE_ENGINE_CONTEXT` defaults to 32768 and the
GPU memory fraction to 0.85. The small default Hugging Face model is a smoke-test
choice, not a promoted vision/tool profile. Configure the model's documented
vision projector, chat template and tool parser before enabling those capabilities.
The llama.cpp example is text-only until a matching projector is configured.
Avoid starting competing GPU engines simultaneously on a single consumer GPU.

Engine images are pinned by release and registry digest: Ollama 0.35.1, vLLM
0.31.0, SGLang 0.5.21, and llama.cpp 0.5.0. Tags and digests were verified on
2026-10-05; host hardware execution remains a separate validation requirement.
SGLang's current image requires a CUDA 13-compatible driver. See the official
[vLLM Docker guide](https://docs.vllm.ai/en/latest/deployment/docker/),
[SGLang installation guide](https://docs.sglang.io/docs/get-started/install), and
[llama.cpp Docker guide](https://github.com/ggml-org/llama.cpp/blob/master/docs/docker.md).

No Docker throughput or hardware compatibility claim is implied by these
configuration profiles. Benchmark the exact model/runtime combination before
promoting an optimization.
