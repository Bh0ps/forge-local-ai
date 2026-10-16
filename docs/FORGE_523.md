# Forge 5.2.3

This revision extends the quieter workspace design through chat, navigation,
model setup and Builder.

## Chat and navigation

The composer uses one compact footer for workflow mode, draft actions, task
controls and performance information. Goal, Plan and other workflow chips use
short labels with distinct icons. Routine saved-draft and ready-state text is
quieter; draft conflicts, missing references, dictation loading and recovery
actions remain visible. Draft storage, attachments and permissions retain their
existing controls.

Connection warnings now take priority over stale working text. When an active
run loses polling connectivity, the footer shows Reconnecting and keeps the
task's controls available. The working indicator returns when polling recovers.

Tools sits directly below Search, ahead of the project tree. Its grouped menu
uses the available viewport space, opens above the trigger when necessary and
refits after resizing. Arrow-key navigation, Escape and focus restoration remain
available. Settings stays directly accessible at the bottom of the sidebar.

Welcome actions are compact and icon-led. Repeated welcome branding and project
text are removed. The roomy message input, project colors, readable conversation
column and subtle activity motion are retained.

## Consistent surfaces

Native model lists, engine editors, measured profiles and Builder use quieter
rows, clear selected states and consistent supporting labels. Nested cards and
duplicate page padding are reduced. Inputs retain their boundaries and keyboard
focus indicators; warnings, connection feedback and verification evidence remain
accessible.

Settings continues to open over the current chat. Workspace accent previews
reflect the actual composer colors. Reduced motion, compact HUD, enlarged text
and narrow layouts are included in the interface checks. Synthetic screenshots
show the actual running components.

## Verification reliability

The native-import integrity regression now advances the fixture's timestamp
explicitly. A same-tick file touch can keep its original NTFS timestamp and
produce an unreliable test result. The fixture still verifies that the runtime
rejects a file changed after verification.

Native model setup, licenses and provider recovery are documented in the
[native model guide](FORGE_520.md) and [5.2.2 notes](FORGE_522.md).
