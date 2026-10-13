# Forge 5.1.7 Windows preview

This preview reduces work in progress polling and tool selection, and loads
Settings when you open it. It retains Forge's chat layout, colors, project tools,
composer modes and connection safeguards.

## Settings stays close to the conversation

Settings loads separately from the initial workspace. Its small loading view lets
you return to chat while the page arrives. A failed load offers a scoped retry;
the conversation, attachments, saved drafts and live activity remain mounted.
Other tool pages retain their shared dependencies so recovering Settings does not
break Browser or another workspace panel.

Retries use a bounded local asset map for the installed build. A map from another
build is rejected. Loading waits during the existing saved-work restart check;
late page completion cannot reopen Settings after you navigate away. Keyboard
focus, narrow layouts, enlarged text, light/dark colors and reduced motion retain
their existing behavior.

## Less work while an agent runs

Progress polling reads the run, events, saved goal and unknown-action status in one
short database snapshot. It computes the public summary after closing the read.
Execution and goal detail views still reconcile the external TODO file; polling
reports committed goal data without repeatedly reading that file.

Tool selection measures each projected schema once per selection, preserving the
same order, limits and exact compact UTF-8 budget. This reduces serialization work
without changing which tools fit. It does not change inference settings or claim
a model tokens-per-second improvement.

## Goal instructions match the tool contract

`goal_update` accepts an existing task ID or exact saved task text. The executor
still rejects added or changed requirements, foreign tasks and stale updates.
For one task, continuation guidance prefers `goal_task_update` with the current
revision from `goal_read`. Helpers keep their assigned scope; the main agent
maintains the overall goal. The compact schema preserves tool availability at
the existing small-context budget.

## Updating and privacy

There is no new database migration in this patch. Temporary helper chats retain
the [automatic cleanup and parent evidence rules](FORGE_514.md), and accepted
connections retain the [5.1.6 authority safeguards](FORGE_516.md). Publisher-verified
GitHub downloads and saved-work restart checks remain in place. Windows
executables do not yet have an Authenticode certificate.

Regression and visual checks use disposable data. Release notes record the
completed full-suite and packaged Windows checks. Private prompts, credentials,
opaque provider state, backups and raw diagnostics are excluded from releases.
