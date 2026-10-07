# Free OpenRouter agents

Forge can assign a research agent to OpenRouter while the main agent uses a local model. Cloud requests have their own queue, so they do not wait for a local GPU lease. Child results return through the existing `agent_result` tool and remain associated with the parent run.

## Configure in Forge

1. Open **Settings → OpenRouter** and enter an API key in the password field. Obtain it from [OpenRouter's key settings](https://openrouter.ai/settings/keys); never paste a key into a conversation.
2. Review **Allow assigned context to leave this computer**. Enabling a cloud agent sends its prompt, selected context, images, tool definitions and tool results to OpenRouter and the selected model provider.
3. Choose whether to allow providers that collect data. The default is off. This restriction can leave no eligible free provider; it does not promise zero data retention. Review [OpenRouter's data collection policy](https://openrouter.ai/docs/guides/privacy/data-collection) and your account's privacy settings.
4. Enable the connection and press **Connect / Test**. This fetches account quota and model metadata; it does not send an inference prompt. Saving a key or opening Settings performs no remote request.
5. Press **Set up helpers & reviewer** to create the Researcher and Assistant profiles and enable delegation plus independent goal review. Existing profile edits and the main engine/context are preserved. You can switch helpers and goal verification off independently. See [goal review](GOAL_REVIEW.md) for verdicts and recovery.

The OS credential vault holds the key. Configuration stores only a vault reference. Forge uses the fixed `https://openrouter.ai/api/v1` endpoint, disables redirects and never accepts a custom OpenRouter authentication endpoint.

## Free-only request contract

Forge accepts `openrouter/free` and advertised model IDs ending in `:free`. Catalog entries must report zero for prompt and completion, every supplied optional price field and any conditional price override. Missing prompt/completion prices or invalid pricing excludes a model. Optional unbilled prices may be omitted, as in the free router's official catalog response. Catalog validation is refreshed before use with a short cache.

Every inference request pins zero maximum prices for prompt, completion, request and image, requires support for all supplied parameters, applies the selected data-collection policy and disables provider fallbacks. Caller-supplied model fallback lists, presets, plugins, transformations and server tools are rejected. Only Forge client function tools are sent. These controls follow OpenRouter's [provider routing contract](https://openrouter.ai/docs/guides/routing/provider-selection).

The [free router](https://openrouter.ai/openrouter/free) chooses among available free models, while an advertised [free variant](https://openrouter.ai/docs/guides/routing/model-variants/free) selects a specific model. A quota or availability failure stays a failure. Forge never silently substitutes a paid model, relaxes privacy controls, purchases credits or automatically retries an inference request.

## Quota, usage and cancellation

Connect / Test displays only the free daily request counters reported by `/key`: used, limit and remaining. Unavailable counters remain unknown. Account labels, email addresses, credit balances and API keys are excluded. OpenRouter's [limits documentation](https://openrouter.ai/docs/api_reference/limits) describes account-dependent limits; free capacity and limits can change.

Usage is recorded once per cloud request, including cancelled or failed requests. Reported token counts are used when available; otherwise Forge labels its counts as estimates. The ledger records the returned model ID, and usage events distinguish the requested router from the actual model and provider. Stopping a run closes the streaming request and releases its cloud queue lease. Partial tool-call fragments are never executed before a complete round.

HTTP authentication and quota errors, malformed streams, incomplete streams and remote error bodies produce bounded local messages. Remote error text cannot enter configuration or run errors, since it may contain private prompt content or credentials.

## Local validation

`test_forge_openrouter.py` uses HTTP fixtures and synthetic keys only. It checks price and privacy guards, metadata redaction, split tool calls, cancellation, authentication and quota failures, independent cloud queue operation, usage accounting and research child results. The suite does not authenticate a real account or perform live inference. A live check requires the user to configure a key and run an explicitly chosen task in Forge.
