# Forge 5.1.22

## Compact, searchable Settings

This preview continues Forge's subtle UI refinement in Settings. It preserves
the current font, warm palettes and existing settings pages, with tighter spacing
and four clear navigation groups: Workspace, Models & tools, Connections and App.

Search matches section names and specific topics, including engine connections,
measured profiles, permission scopes, browser pairing and update recovery. The
current section stays visible even when it does not match the search. Typing a
query does not change the open page; choose a result to navigate. Narrow windows
retain a compact section selector.

Search keeps the current form mounted. It preserves pending engine Test and Save
requests, entered credentials and model-download text. It does not start tools,
save settings, load matching deferred pages or expand optional browser setup.
Clear search with its labeled control or Escape while the search field has focus.

Keyboard focus labels also survive the initial browser scroll that brings a
control into view. A brief, single-frame focus allowance preserves that label
without moving focus. Explicit input retires the allowance; ordinary scrolling
and visibility changes dismiss labels. Escape and activating the control also
dismiss its label. Labels do not reappear when a hidden window is reopened.

The chat and composer controls introduced in 5.1.21 remain available. Navigation
and right-pane refinements continue in later passes; this release does not claim
that every page has already been redesigned.

## Validation

Regression checks cover explicit keyboard navigation, matching topics, the
current-section marker, form ownership, pending requests, lazy loading, optional
disclosures and focus-scroll label cancellation. Production checks use synthetic
data to inspect layouts, focus and contrast. Full frontend, Windows CI and sealed package checks gate
publication. No model or startup-speed benchmark is claimed.

Personal state and credentials stay outside the public source and release assets.
Verified GitHub downloads and the installation guard for unfinished work remain
in place.
