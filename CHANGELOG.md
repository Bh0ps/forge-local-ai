# Changelog

## 4.2.3

- Local agents discover enabled OpenRouter helper IDs, models and scopes,
  delegate bounded tasks, and receive waited results without repeated polling.
  Explicit setup preserves edited profiles and the selected local engine.
- Independent read-only OpenRouter goal review checks original requirements,
  actual evidence and task coverage. Validated verdicts approve completion,
  return specific repair work, or pause an unverified goal with recovery.
- Durable review admission, evidence scopes, replay-safe feedback, shared limits
  and separate usage accounting preserve work across cancellation and restart.
  No paid fallback or automatic quota retry is introduced.
- OpenRouter settings separate helper and reviewer controls. Goals and Activity
  display review verdicts and feedback, including paused unverified goals.
- Small-context goals retain journal tools; interrupted child agents cannot
  overwrite the main goal status. Structured output failures cannot become success.

## 4.2.2

- Redesigned Library with Discover and Installed views, instant shared search,
  type/category filters, source status and clear compatibility/setup details.
- Ten MIT starter skills are available immediately, with individual enabled and
  automatic-selection controls. Relevant guidance reaches the agent and switches
  are reloaded between rounds. Local edits and saved selections survive upgrades.
- A bounded official collection adds portable OpenAI plugins and Apache-licensed
  Anthropic skill examples alongside connected catalogs and local imports.
  Restricted document packages are excluded; discovery never installs scripts.
- External imports preserve selected Git folders, immutable commits and licenses,
  with staged review and explicit requirements for unsupported host components.

## 4.2.1

- Build and `/goal` continue a reviewed plan with an explicit implementation
  request and a durable goal checkpoint. Goal/chat admission commits atomically;
  inherited child runs cannot replace the main goal's identity.
- One `/todo` palette entry; the older `/to-do` spelling remains compatible.
  The Goals panel contains only active goals whose conversations still exist.
- Mid-run text steering interrupts generation, preserves partial replies and
  waits for started tool actions before changing direction. Unstarted actions
  and pending approvals are superseded, with no repeated completed writes.
- Selectable agent questions include a recommendation and custom answers.
  Questions survive pause/restart and become answerable only after tool results
  are paired. Answers never grant action permissions.
- Latest steering and answered choices remain exact across context compaction.
  Accepted human inputs appear during the run without duplicating saved replies.
- Desktop launch is maximized; HUD/Expand and tray reopening preserve it.
  Memory review/search is on the left, with categories and preferences on the right.

## 4.2.0

- Guided hardware, engine and model setup with resumable downloads and explicit
  coding, tools, vision, browser and microphone checks.
- Reviewable project/agent/global memory, keyword recall, optional local CPU
  embeddings, correction, forget/export and edited reusable skill proposals.
- Paired Telegram tasks, exact action approvals, notifications and authenticated
  webhook ingress. Connected tasks retain their permission ceiling as settings
  change; duplicate incoming updates never create a second run.
- Optional OpenRouter free research agents with explicit remote-context consent,
  zero-price routing, a separate inference queue and per-request usage.
- GitHub repository browsing, credential-safe managed cloning and scoped tools.
  Creating a draft PR requires an exact human approval, including under Full Access.
- Embedded Windows WebView2 browser in the workspace sidebar, sharing the same
  page with agent tools. Explicit Chrome/Edge tab connections use scoped identities.
- Brain button for context presets beside dictation, stable backend streaming
  speed estimates, correct project-root file browsing and one activity card per
  tool invocation.
- Atomic run completion, channel-safe chat deletion, isolated goal Coder workspaces
  and remote compaction failure recovery without automatic quota retries.
- Continued streaming across replayed completion records and rediscovery of
  chats resumed from another client, preserving consumed event cursors. Saved
  assistant rounds are reconciled without removing legitimate repeated answers.
- Hash-locked Windows CI, installer/portable build stages, dependency notices,
  privacy-safe diagnostics and signed-update verification/rollback infrastructure.
  Public packages remain unsigned while signing setup is deferred.

See [validation](docs/VALIDATION.md) for executed checks and deployment limits.
