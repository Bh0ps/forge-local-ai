# Forge 5.1.23

## Project navigation and workspace tabs

This preview continues Forge's subtle UI refinement in the sidebar and workspace
pane. New chat, local search, projects and their conversations retain the main
navigation space. Existing Tools moves below the tree, with quiet Work, Manage and
Inspect groups. Settings stays directly accessible.

Project rows give the saved icon and accent a clearer selection cue. The existing
search, collapse, archive and conversation actions keep their current scope;
hovering a project or menu item does not change its saved icon or accent, or open
a tool.

The workspace pane has a compact heading and five visible icon tabs: Context,
Browser, Activity, Goals and Files. Hover or keyboard focus shows the tab name and
description, and the selected tab's heading remains readable. Arrow keys, Home
and End retain tab navigation. Existing saved activity, helper results, goal
recovery, files and browser controls remain available.

Tools retains each supplied destination and its keyboard behavior. Its menu opens
above the footer control and scrolls within the available height. Stable action
identities prevent a focused button from becoming a different destination when
supplied rows are reordered.

The current font, warm palettes, roomy composer, pane sizes and saved layout
preferences are preserved. HUD and reduced-motion behavior remain in place. No
new operational cards, destinations or appearance settings are introduced.

## Tray and workspace lifecycle

Open Forge, Stop all agents and Quit Forge use separate, coalesced workers so a
slow window or coordinator operation cannot block the tray menu. Open restores
and focuses the existing workspace through the bounded native dispatcher. Quit
claims teardown once, removes the tray icon before coordinator cleanup, and
preserves browser, service and window cleanup ordering.

The tray registers after the native workspace exists. Its readiness callback
checks shutdown again so a delayed icon cannot appear after Quit. Late window
load and visibility callbacks leave a closed host alone. A pending restart keeps
its existing cancellation requirement, with a fixed tray notice and Open action.

Closing the workspace still hides it to the tray. If the native workspace is
actually destroyed unexpectedly, its closed event invalidates the bridge and stops the owned
coordinator instead of leaving an unusable tray icon. Native shutdown waits for
the cleanup owner before the desktop process returns.

Regression fixtures cover a stalled Open action followed by Quit, concurrent
Quit requests, repeated tray clicks, close cancellation, and lost-window cleanup.
The packaged Windows check exercises the real close event and tray shutdown.

## Native verification diagnostics

The disposable Windows smoke check records a bounded final observation when
waiting for an owned standalone preview. Diagnostics contain fixed booleans and
an iteration count, including visibility, ownership, valid matching bounds and
stable samples. They contain no identifiers, coordinates, paths, URLs, titles,
chat text, credentials or raw exception messages.

The existing readiness deadline, two-sample check, ownership restrictions and
nonzero-process-exit rejection remain in force. This diagnostic addition does
not claim to fix the underlying preview-readiness failure; fresh Windows checks
are required to establish its cause and verify the final release.

## Validation

Checks cover Tools grouping and keyboard navigation, project selection cues,
the five real tab controls, existing Activity focus guards and unchanged callback
ownership. Production fixtures use synthetic content to inspect contrast,
clipping, enlarged text, narrow layouts, visibility and retained drafts. Current
full frontend, Windows CI and sealed package checks gate publication. No model
or startup-speed benchmark is claimed.

The separate history-loading and late-steering focus fixes follow in the next
chat revision. This preview does not claim that every feature page has already
been redesigned.
