# Forge 5.1.21

## Chat and control foundation

This preview begins Forge's subtle UI refinement with everyday chat and header
controls. It retains the existing font, warm palettes, 760px conversation column
and 800px composer.

The composer keeps a roomy text area and groups attachment, model/permission and
action controls. Model names and permission state remain readable. Familiar icon
actions use clearer outlines and reveal the same visual label on hover or keyboard
focus, while the actual button retains its accessible name.

Label presentation does not start tools, capture the screen, navigate pages or
move focus. Labels dismiss on relevant visibility and input changes. Disabled,
active, hovered and focused controls remain distinguishable, with compact HUD and
narrow-window exceptions preserved.

Affected rules are consolidated in the existing stylesheets. The existing
activity glow and motion preferences are retained; streaming text receives no
decorative animation. Drafts, attachments, dictation, steering, slash modes,
permissions, recovery and the explicit Activity focus transfer remain intact.

This is the first coordinated pass. Compact searchable Settings, navigation
organization and right-pane detail structure follow separately. All existing
right-pane tabs remain available.

## Validation

Interaction checks cover label lifecycle and button semantics without additional
API calls. Production checks use synthetic data to inspect light/dark surfaces,
keyboard focus, narrow and enlarged layouts, HUD behavior, contrast, control
targets and label clipping. Current full frontend, Windows CI and sealed package
checks gate publication. No model or startup-speed benchmark is claimed.
