# Forge 5.1.20

## Keyboard focus when opening Activity

Opening **Activity** from Desktop evidence now transfers keyboard focus to the
selected Activity tab. Previously, the activating button disappeared as the pane
switched, leaving focus on the document body and interrupting keyboard navigation.

The transfer uses the existing workspace tab references and layout lifecycle.
It belongs to that explicit user action and can be consumed once. Navigation,
scope changes, hidden panes, lost focus or an update lock discard the request;
reopening a pane or unlocking the workspace cannot replay it. Agent status updates
do not request this focus transfer.

The tab layout, colors and current selection behavior are preserved. Existing
arrow-key navigation, saved-image controls and permission safeguards remain in
place. This change does not activate a native application or add computer input
capabilities.

## Validation

The defect was reproduced in the actual production app with synthetic Desktop
evidence at wide dark and narrow light sizes. The existing arrow-key tab route
served as a positive control. Regression and production checks cover the explicit
Activity route, current ownership and cancellation of stale focus requests.
Current full Windows CI and sealed package verification gate publication.
