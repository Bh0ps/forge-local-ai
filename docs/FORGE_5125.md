# Forge 5.1.25

## A clearer Library

Library distinguishes a collection that is loading, a failed request, an empty
collection and filters with no matches. A failed request no longer suggests that
you have no installed skills. Explicit refresh keeps available cached items
visible, and the existing retry controls remain available.

Filters use compact groups with clear selection states and readable labels.
Item actions retain their names and gain the relevant item description for
assistive technology. Skill instructions have more readable text and spacing,
including code, headings and long content in the detail view.

The existing review, import, install, enable, automatic-use and workbench actions
keep their current scope. Reading instructions does not install or enable a
skill. Agent permissions still control every tool action.

This preview includes the preceding tray lifecycle and chat focus fixes. It
retains the grouped composer, saved drafts, workspace layout, warm palettes,
keyboard navigation, HUD and reduced-motion behavior.

## Validation

Regression checks cover initial loading, transport failure, empty collections,
filter results, cached refresh, detail controls and keyboard focus. Synthetic
production fixtures cover both themes, five accent palettes, narrow and enlarged
layouts, and instruction dialogs. Full frontend, Windows CI, packaged lifecycle,
installer and privacy checks remain required before publication.
