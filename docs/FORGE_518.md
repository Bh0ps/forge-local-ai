# Forge 5.1.8 Windows preview

This preview keeps the chat reading experience steady and makes Settings recovery
clearer. It retains Forge's layout, colors, project tools and connection safeguards.

## Keep your place in model reasoning

Your choice to open, close or scroll a reasoning disclosure carries into the saved
assistant message. Choosing an earlier part keeps automatic
following paused; Jump to latest returns to the newest text. A closed disclosure
stays closed. Keyboard focus stays with the same reading region through the
transition when it held focus. Streaming characters receive no decorative typing
animation.

Reading intent is scoped to the exact conversation, run and assistant turn. It
does not carry into unrelated messages or ambiguous historical turns. The bounded
memory contains reading controls and positions, not copies of reasoning text or
private provider state. Existing glow, reduced motion and hidden-window behavior
retain their styling and controls.

## Clear recovery after failed Settings loads

Settings retains the three explicit retries and the installed build's local asset
checks. After those retries fail, Forge removes the ineffective retry button and
explains that chat remains available and Settings can be tried after reopening
Forge. Return to chat and local Escape preserve the draft and keyboard focus.

Ordinary loading and successful retry behavior stay the same. The page does not
reload the workspace, reset its retry budget or interrupt running work. Existing
saved-work restart checks still apply when the user chooses to reopen the app.

## Updating and privacy

There is no new database migration. This patch retains [5.1.7 runtime improvements](FORGE_517.md),
[accepted provider safeguards](FORGE_516.md), automatic temporary helper cleanup,
and publisher-verified GitHub downloads. Windows executables do not yet have an
Authenticode certificate.

Tests and visual checks use disposable data. Release notes record the completed
full-suite and packaged Windows checks. Private prompts, credentials, opaque
provider state, backups and raw diagnostics remain outside releases.
