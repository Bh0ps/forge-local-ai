# Forge 5.1.16

Older saved connections may use `openai` or `compatible` as their protocol kind.
They already work for inference, but the current connection editor uses
`openai-compatible`. Previously, testing that same saved endpoint could omit its
authentication, and saving it without a replacement key could lose the key binding.

Forge now recognizes the supported compatible-protocol aliases when deciding
whether Test or Save can retain the existing credential reference and preset.
Leaving the key field blank works as described for the unchanged exact endpoint.
Credentials remain in the operating system's credential store.

Endpoint changes and actual protocol-family changes still require their own key.
Missing saved credentials, stale profiles, cancellation and queued-work authority
changes retain their existing recovery paths. The exact durable run binding is not
normalized: changing a profile does not authorize previously queued or interrupted
work, and completed tool actions are not replayed.

The correction uses the existing connection editor and adds no settings or visual
controls. Docker Model Runner and its honest context/reasoning/tuning labels remain
available. Existing 5.1.15 screenshots show the preserved editor.

Regression checks use disposable state, fake credentials and mocked HTTP. They
exercise the actual Test, Save, discovery and run paths; no personal credentials
or live engine content are included in the fixtures or public assets.
