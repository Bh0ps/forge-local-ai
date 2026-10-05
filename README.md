<p align="center"><img src="assets/forge.svg" width="96" alt="Forge"></p>

# Forge 4.2.2

**A local AI workspace for coding, research and computer tools. Open source under [MIT](LICENSE).**

Forge is the next version of Sidekick. It combines a Windows desktop workspace,
an always-on-top HUD and an authenticated local browser interface around one
Python coordinator. Models run through your existing Ollama installation or a
configured local inference engine. No cloud AI account is required.

The 4.2 upgrade adds guided setup, reviewed persistent memory, Telegram tasks
and notifications, optional free OpenRouter research agents and an explicit
GitHub connection. The native agent browser is embedded beside Files and
Activity. Context presets sit beside dictation; live generation speed comes
from backend timing rather than text arriving at the interface.

The 4.2.1 correction makes Build start a tracked goal with clear implementation
instructions, adds mid-run steering and selectable questions, and cleans up
active goals and the Memory layout. Desktop launches are maximized.

The 4.2.2 library adds ten ready-to-use, toggleable skills and a searchable
Discover collection of portable skills and plugins. Source, license and setup
details stay visible during review; the starter collection works offline.

Cloud integrations are optional and require explicit setup. OpenRouter agents
use free-only routes with no paid fallback, subject to provider availability
and quotas. GitHub draft PRs always need approval. Windows packages are currently
unsigned; automatic installation remains disabled until a trusted publisher is
configured. Existing installations and local state are retained for rollback.

[Download Windows packages](https://github.com/Bh0ps/forge-local-ai/releases)
· [Quick start](QUICKSTART.md) · [Integrations and Docker](docs/INTEGRATIONS.md)
· [Architecture](docs/ARCHITECTURE.md) · [Validation](docs/VALIDATION.md)
· [Guided setup](docs/SETUP.md) · [Memory](docs/MEMORY.md)
· [Telegram and webhooks](docs/CHANNELS.md) · [OpenRouter](docs/OPENROUTER.md)
· [GitHub](docs/GITHUB.md) · [Updates](docs/UPDATES.md)
· [Plans, steering and questions](docs/WORKFLOW_INPUT.md)
· [Skills and plugins library](docs/LIBRARY.md)

## Workspace

- React/TypeScript interface with system light/dark appearance, projects and
  chats, Spaces, Scheduled, Library, Agents, Usage and Settings.
- Full workspace and compact HUD share the same chat, draft and attachments.
  Reasoning cards show reasoning supplied by the model.
- File previews, tool activity, command output, diffs and durable Markdown goal
  checklists live in the right panel.
- Searchable model chooser beside the composer; install/delete controls are in
  Settings. Hugging Face search, GGUF downloads and Ollama import use managed local
  folders with progress, cancellation and compatibility checks. Context presets
  and the slider cover 2K–256K, initially 32K.
- Chat menus support moving, archiving and deleting; project removal preserves
  folders and moves chats to Unassigned. Both menus support right-click.

## Agents and tools

- Project reads, writes, backups, conflict checks, bounded commands, public web
  research, local Windows accessibility/screenshot tools and a native WebView2
  browser. An isolated Playwright browser and explicit existing-tab extension
  connections remain available.
- **Always Ask** initially permits connected-project reads and asks before edits,
  commands, computer actions and unknown MCP operations. Full Access and Deny
  Access, plus project/app/server/tool overrides, are configurable.
- MCP stdio, Streamable HTTP and legacy SSE through the official SDK, with bearer
  and OAuth authentication. Windows Credential Manager holds credentials.
- Global/project skills, reviewed plugin imports and linked catalogs. Portable
  Codex/Claude skills and MCP definitions are supported; host-specific components
  display adapter requirements before installation.
- Editable Researcher, Coder and Reviewer profiles. Writing agents use Git
  worktrees; changes are reviewed before integration. Non-Git writers serialize.
- Timezone-aware schedules with overlap prevention and one catch-up after downtime.
  Closing the desktop window keeps the tray coordinator running. **Quit Forge**
  stops it. Windows startup is disabled initially.

## Continuity and usage

`/plan`, `/todo` and `/goal` share the command registry with `/pause`, `/resume`,
`/status`, `/compact`, `/new`, `/project`, `/model`, `/agents`, `/worktree`, `/skill`,
`/mcp`, `/schedule` and `/help`.

`/plan` saves a reviewable Markdown plan without editing the project. Select Build
or enter `/todo` (also `/to-do`) in that chat to start its ordered goal checklist.
`/todo` followed by tasks creates and starts a new checklist. The agent reloads
saved progress after compaction and restart and records completion evidence.

Each run retains its exact request, settings, event cursor and tool outcomes.
Compaction runs between completed rounds. Full tool results remain retrievable
artifacts; summaries use bounded excerpts and a deterministic checkpoint if
summary retries fail. Truncated responses receive two bounded continuation
attempts; unfinished tool calls are never executed. Context and goal limits pause
with Resume available.
Interrupted side effects require outcome inspection before continuation.

Goal checklists are saved at `~/.forge/state/goals/<id>/TODO.md`, with ordered tasks,
evidence, checkpoint, blockers and next action. External edits require reconciliation.
The default shared goal limits are 60 minutes, 100,000 generated tokens, 128 model
rounds and 256 tool invocations.

Usage records begin with this upgrade and include main/child requests, compaction
and retries. Daily, calendar-month and all-time totals distinguish reported and
estimated counts. Completed tokens/second uses backend decode time; streaming
estimates are labeled. Raw timestamps remain UTC.

## Local inference

Existing Ollama installations remain usable. OpenAI-compatible local endpoints
support llama.cpp, vLLM and SGLang. Optional verified managed runtimes can compare
full-precision and q8 KV caches with coding, streamed-tool and image probes.
Speculation, MTP/n-gram methods, CUDA graph tuning and native NVFP4 checkpoints are
gated by the exact executable, hardware and model configuration, then require
validation before activation. A listed engine feature is not a speed guarantee.

Forge queues one inference request per GPU, prioritizes foreground work, retains
warm models where supported, bounds active history and selects tools within the
context budget. It never silently changes an explicit model or context selection.
Cold model loading and prompt processing can wait up to one hour for the first
response. Once output or tool-call progress starts, three minutes without progress
pauses the request; the overall request ceiling is 90 minutes. Stop interrupts
either wait promptly, and timed-out runs retain their checkpoint for Resume.
CPU INT8 faster-whisper dictation inserts an editable transcript; raw audio is
kept in memory and discarded. Native browsing uses the existing Windows WebView2
runtime; speech models and the optional isolated browser are explicit downloads.
Performance shows live RAM/VRAM, loaded models, warm/release controls and measured
coding, image and tool probes. Model/context recommendations require acceptance.

## Run from source

Use Python 3.12, Node.js 22.12+ and Windows WebView2. Start Ollama separately.

```powershell
python -m venv .venv
& ./.venv/Scripts/python.exe -m pip install -r requirements-dev.txt
cd frontend
npm ci
npm run build
cd ..
& ./.venv/Scripts/python.exe desktop.py
```

For the local browser coordinator, run `model_manager.py` instead. Open
`http://localhost:8081` and enter the one-use pairing code printed in that terminal.
Native clients use the authenticated bridge. HTTP APIs require a paired cookie or
bearer session and enforce same-origin requests. Do not expose the coordinator on
the public internet.

## Build and test

```powershell
& ./.venv/Scripts/python.exe -m pytest -q
cd frontend
npm test
cd ..
./build.ps1 -Iscc 'C:/Program Files/Inno Setup 7/ISCC.exe'
```

The build produces a portable ZIP, and an installer when Inno Setup 7 is available.
The portable executable needs its `_internal` directory beside it. Packages include
dependency license files; see [third-party notices](THIRD_PARTY_NOTICES.md).
Build artifacts and personal state are excluded from Git.

## Data and rollback

Forge separates configuration, SQLite state, attachments, backups, projects,
worktrees, skills, plugins, runtimes and artifacts beneath `~/.forge`.
`FORGE_DATA_DIR` can select a separate home. Existing project folders are registered
in place; new managed projects and worktrees live beneath that home.

First startup takes a consistent SQLite backup and imports existing Sidekick
projects, chats, tasks and context settings without changing IDs or external
project paths. Old installation/data remain intact. Backup manifests and attachments
are copied. Keep the pre-upgrade backup before restoring or downgrading. Runtime
models, plugin content and personal conversations are never bundled in releases.

Forge and its permission profiles are a local application boundary, not an OS
sandbox. Commands execute with your user account's permissions. Model quality,
context capacity and inference speed depend on the model and hardware. See the
validation report for measured results and untested deployment configurations.
