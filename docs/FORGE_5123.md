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
