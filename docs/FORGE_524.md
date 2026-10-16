# Forge 5.2.4

This revision makes the everyday workspace quieter and more consistent while
keeping Forge's colors, project identity and existing workflows.

## Conversation and controls

Assistant messages keep their author available to screen readers without
repeating the Forge heading throughout the conversation. Message actions appear
on hover or keyboard focus and remain visible on touch screens. Copy feedback
stays visible long enough to read, and completed or failed clipboard operations
cannot update a retired message.

Saved messages and unchanged Markdown skip unnecessary renders during streaming.
Empty cursor polls retain their existing state, including when the coordinator
returns fresh copies of unchanged progress and planner information. Hidden
windows poll less frequently; returning to the workspace promptly resumes cursor
replay. The coordinator continues its work throughout.

Newly opened conversations use measured message layout instead of deferred
placeholder heights, so the initial position reaches the latest response.
Jump to latest sits inside the conversation viewport, above the composer. Its
position follows the available space when attachments, approvals, larger text or
a multiline draft increase the composer's height. Reasoning auto-follow, manual
reading position, history anchoring, code copy and durable activity remain in
place.

## Navigation and detail pane

Searching projects temporarily opens matching groups with accurate expansion
controls. Collapsing search results affects only that query, preserving the
workspace's saved project preferences.

The five compact detail tabs remain visible, with a short heading identifying
the current view. Reading positions belong to the selected tab and conversation.
Pointer resizing publishes at most once per animation frame, keeps the final
position and retires pending work on cancellation or unmount. Keyboard resizing
and saved layout acknowledgements are retained.

## Skills, notes and Settings

Library presents compact bundle groups with one main action per item. Details
retain source, compatibility, license, enable controls, automatic-use preferences
and the workbench. Plugin inspection and reviewed installation are unchanged.
Library and Context share bundle names and aliases, including custom bundles.

Notes use a compact disclosure with a count; matching search results open
automatically. Attached skills and notes have one removal action each, with
keyboard focus preserved after the item disappears.

Theme now sits with workspace appearance. Switching Settings sections returns to
the beginning of the new section without changing preferences. Supporting switch
descriptions are associated with their controls, and project icons have readable
hover and focus labels.

Superseded Context styles are removed, foundational styling loads before feature
styles, and shared motion timing keeps panels and controls consistent. Reduced
motion disables transitions; hidden activity indicators stop animating.

## Validation boundaries

Automated regression checks exercise cursor recovery, reasoning ownership,
keyboard focus, resizing, sidebar search, bundles and reference removal.
Production-interface captures use synthetic data and isolated browser profiles.
These interface changes do not claim an increase in model generation speed.
Publication and replacement of a running installation remain separate steps.
