# Forge 5.1.1 Windows preview

This patch keeps Forge's colors and workspace layout while making everyday
conversation, settings and connection setup easier to use.

## Thinking and workspace details

Live reasoning follows the newest text. Scroll back to read earlier text and
following pauses; **Jump to latest reasoning** resumes it. Your expanded or
collapsed choice survives answer completion. A small breathing accent indicates
active reasoning, with no animation on individual characters. Hidden windows
pause it, and reduced motion removes continuous animation.

The right pane has compact, labeled icon tabs and keyboard navigation. Activity
shows the current chat or project's saved requests and nested helpers, rather
than unrelated runs from other projects. Completed history remains visible;
recovery and interrupted-action inspection stay accessible outside collapsed
tool details. Browser widths and short-window bounds remain independently checked.

## Settings and connections

Settings is grouped into Workspace, Models & tools, Connections and App. Search
by a setting's name or purpose; narrow windows use a compact section selector.
Temperature and goal limits remain available under Advanced conversation settings.

Testing an engine checks an unsaved draft and reports a clear connection result.
It does not create an engine or save a typed API key. Cancelling the dialog ignores
late responses, and testing and saving cannot overlap. Saved authentication is
reused only for the same engine type and complete endpoint URL. Editing an engine
preserves its configured capabilities, context and resource settings.

Configure OpenRouter through its dedicated settings. For a custom Ollama address,
add a separate endpoint and select it as the active engine; the built-in default
connection remains available.

Changing an MCP server's target removes its old credential links and discovered
tool catalog. Test the new target before enabling it. Existing credentials for
an unchanged target remain bound to that target. No authentication disables
automatic bearer/OAuth headers; explicitly configured custom API-key headers and
stdio environment credentials retain their separate, scoped behavior.
Removing a credential link keeps its encrypted vault value available to other
configured integrations and local recovery snapshots.
Tool and resource requests are checked against the current account, discovered
catalog and permissions before dispatch. Connection-readiness failures report
that no tool request was sent; uncertain outcomes after dispatch remain available
for inspection. Remote SDK exceptions do not echo private connection details.

## GitHub update detection

The installed Windows app checks the public GitHub release feed in the background
after startup and periodically afterwards. Choose Stable or Preview in Updates
settings, or turn automatic checks off. Failed checks retry after a bounded delay.
Manual checks return immediately while the request runs; hidden settings pages
do not continuously poll. Source launches and disposable smoke tests make no
automatic public-feed requests.

Preview detection uses published releases because GitHub's latest-release
endpoint excludes previews. Checks never execute an unsigned download. This
release remains an **unsigned Windows preview**; automatic installation still
requires independently authenticated publisher artifacts. Use the public installer
link for previews while that signing work is completed. Failed, cancelled or
interrupted checks cannot leave a stale Install action armed.

## Validation and recovery

The release gate runs backend tests in four exhaustive Windows shards, frontend
tests, TypeScript and production compilation, and native workspace, Library, HUD,
research and isolated preview checks. Published packages are compared with the
tested source and checked for personal state, private paths and credentials.
Exact results are recorded in the [release notes](https://github.com/Bh0ps/forge-local-ai/releases/tag/v5.1.1).

There is no new database migration in this patch. Existing schema-7 workspaces,
chats, models and configured preferences remain supported. Back up state and close
Forge before replacing its installation. Earlier schema-6 applications require
restoring the pre-schema-7 backup described in the [5.1 recovery notes](FORGE_51.md#verification-and-recovery).
