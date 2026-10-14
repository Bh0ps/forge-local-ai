# Forge 5.1.12 browser ownership and retained activity

This preview refines the existing Browser/Desktop workspace with safer pane
ownership, a quiet browser target cue and durable helper computer activity.
It keeps Forge's colors, chat layout and project workflows.

## Browser actions respect pane ownership

The native research browser requires the current UI to admit its visible pane
using validated bounds. Hide revokes its private ownership generation before
queued I/O runs. Earlier Show, resize and agent requests cannot reclaim it.
Stop, host hiding and changed ownership invalidate snapshots and queued work.

Research agent navigation and DOM actions use the admitted pane without host
Show, Activate or Focus calls. Direct user controls retain their explicit UI
behavior. Builder previews and connected extension tabs keep their own scopes.
An action revoked after dispatch remains uncertain and needs inspection before
another write; completed actions are never replayed.

A disposable Windows source test verified one literal field edit and one click
while another window remained foreground. Its pointer stayed unchanged and
neither action called the host Show method. This is evidence for those native
DOM operations. Page handlers can still affect their own application behavior;
shared Windows desktop tools retain their documented focus/pointer boundaries.

## A restrained target cue

Permitted research-browser interactions can briefly mark their numeric target
with a stationary outline. The existing toolbar shows one small `AI target`
label only for the current conversation/request or a proven helper descendant.
The cue carries no selectors, field values, titles or raw errors. It clears on
Stop, hiding, ownership/navigation changes, inspection and screenshots, and
expires locally even if a status request stalls. Reduced-motion behavior stays
static. The cue records the interaction target; fresh evidence must establish
the requested result.

The browser recipe includes lazy examples for literal edits, hidden-pane
recovery and uncertain submissions. Its examples validate against current tool
schemas and fit the existing context budget. Starter updates preserve local
customizations and disabled choices; guidance grants no additional permissions.

## Helper activity survives conversation retirement

Consumed temporary helper conversations and their journals/provider-state rows
continue to retire after their parent's completed work. A small coordinator
summary now retains completed computer-step counts and the latest tool/time.
It is committed atomically with retirement and bound to exact run/chat ancestry.

The Desktop view preserves those counts and its parent's honest status after
cleanup and reopening. Existing invocation rows suppress duplicate retained
counts. Malformed, foreign or changed-ownership summaries contribute no data;
counts are capped with a truncation flag. Older retired records without this
metadata stay unknown.

The existing last-step area identifies retained helper activity. A retired
summary contains no arguments, entered text, clipboard values, app titles,
errors, pixels or snapshot authority. A usable snapshot still requires a
surviving acknowledged inspect invocation and its exact artifact. Existing
artifacts remain retained. This view performs no capture or native input.

The [previous refinement](FORGE_5111.md) and [computer-tool guide](FORGE_5110.md)
describe the model checks, saved-snapshot controls and desktop boundaries.
