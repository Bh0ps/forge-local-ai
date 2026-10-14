# Forge 5.1.15

Docker Model Runner joins Forge's local engine presets. The existing connection
editor provides its endpoint, test action, model refresh and official setup guide.
No additional settings page or visual style is introduced.

## Connect Docker Model Runner

Enable Docker's host TCP access and prepare a local GGUF model using the
[official Docker instructions](https://docs.docker.com/ai/model-runner/get-started/).
In Forge, choose **Docker Model Runner** and use the explicit loopback llama.cpp
route, initially `http://127.0.0.1:12434/engines/llama.cpp/v1`.

Forge reads the bounded local catalog and sends inference to that configured
route. A fresh catalog must contain the selected exact model identifier before
inference starts. Edited non-loopback addresses, default multiplex routes, cloud
catalog descriptors, redirects and invalid catalogs cannot authorize this local
adapter. The saved profile is never silently rewritten.

The catalog tag and the filename reported by the inference stream remain separate.
Forge does not guess aliases or claim that a package tag proves the loaded model's
identity. Completed tool evidence survives continuation and restart; catalog
refresh does not replay completed actions or grant additional permissions.

## Honest model settings

Docker's package context hints do not establish its active serving context. Forge
keeps capacity, loaded instances, vision and tool training unknown when the engine
does not report usable facts. A user-configured context limit stays labeled as
configured. Automatic tuning, warm/release controls and reasoning controls remain
unsupported by this adapter; saved global preferences are preserved.

Connection and metadata failures use bounded diagnostics, retaining authentication
status without exporting response bodies or raw transport errors. The setup link
accepts the exact official HTTPS host and rejects HTTP and lookalike hosts.

## Validation

Offline tests use disposable profiles, synthetic catalogs and mocked HTTP streams.
They cover endpoint scope, catalog limits, cancellation and profile revocation,
multi-round tools, restart, no completed-action replay and private error handling.
They do not claim a live Docker installation or measured model throughput.

The frontend keeps the existing engine editor and is checked with TypeScript,
the production build and scaled light/dark fixtures. Release checks bind the
compiled application and assets to the exact source and publisher signature.

All previous chat, Builder, computer-use and verified GitHub update workflows remain
available. Desktop actions still share Windows focus and the system pointer.
