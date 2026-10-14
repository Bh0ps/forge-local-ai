# Forge 5.1.24

## Keep chat focus with the reader

This preview corrects two chat focus problems while retaining Forge's current
layout, styles, run requests and draft handling.

When you queue a direction for a running agent, its delayed reply can restore
focus only while that explicit action still owns the current chat. Switching
chats or focusing another control retires that request. A reply to the old run
cannot pull focus into the newer chat's composer or overwrite its draft.

Keyboard history loading keeps a local reader destination. When more history
remains, focus returns to the re-enabled loading control. When the final page
removes it, focus goes to the conversation-history region without scrolling.
The existing history anchor and pagination request remain in place.

Focus restoration is limited to one explicit action and the same page, chat,
project, run, request generation and connection. Later interaction, navigation,
visibility changes, an update lock or a replaced connection retires it. Reopening
the window or clearing a lock does not replay a cancelled focus request. Agent
status and background output do not initiate focus restoration.

## Validation

Regression checks cover remaining and final history pages, delayed steering,
typed drafts, different chats and controls, stale responses, keyboard activation
and cancellation. Current full frontend, production layout, Windows CI and sealed
package checks remain required before publication. Fixtures use synthetic data;
no installed-session or model-speed claim is made.
