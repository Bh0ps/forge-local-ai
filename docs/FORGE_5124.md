# Forge 5.1.24

## Keep chat focus with the reader

This preview corrects two chat focus problems and tightens native window action
completion while retaining Forge's current layout, styles, run requests and
draft handling.

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

## Wait for Windows to finish the action

Marshaled window actions retain their Windows callback result until managed
completion is confirmed. The existing five-second wait budget remains bounded.
A Python callback finishing its work alone is insufficient evidence that the
managed callback has returned.

When completion cannot be confirmed, Forge reports an uncertain outcome and
retains the pending operation. It does not automatically replay the action or
advertise that native input is idle. A later probe can retire a callback whose
completion is known. Direct calls on the Windows UI thread remain synchronous.

These changes address a reproduced callback-ordering defect. The intermittent
CLR shutdown crash observed in package testing has not been attributed to a
confirmed runtime cause. A failed Windows package check blocks publication.

## Validation

Regression checks cover remaining and final history pages, delayed steering,
typed drafts, different chats and controls, stale responses, keyboard activation
and cancellation. Native regression checks cover managed completion, deadlines,
uncertain results and operation lifetime. Current full frontend, production
layout, Windows CI and sealed package checks remain required before publication.
Fixtures use synthetic data; no installed-session or model-speed claim is made.
