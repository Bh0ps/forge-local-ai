# Forge 5.1.13 — quieter polling and clearer controls

This preview removes unnecessary browser-status reads and improves two existing
controls while retaining Forge's warm colors, layout and computer-tool safeguards.

## Browser status

Expired, hidden, stopped, loading or stale browser target cues skip run-ancestry
lookups. A fresh cue still verifies the exact requested chat/run on each status
request. There is no authorization cache. The result is checked again after the
read so Hide, Stop, navigation, replacement or expiry during a slow read cannot
publish an obsolete cue.

A disposable SQLite fixture reproduces ten ancestry connections across ten
expired-cue polls in 5.1.12 and zero in this revision. This measures avoided
database reads; it is not a model throughput measurement. Reading status sends
no native input or browser interaction.

## Existing controls

When the exact, untruncated computer-step total consists entirely of retained
helper steps, the Desktop view shows one `N recorded helper steps` line. Mixed
activity and bounded totals preserve the separate total and retained meanings.
Invalid metadata is not presented as an exact count.

The selected-model button now has an accessible name containing its exact model
ID and exposes its selection state. Manual model selection, permission controls
and the connection workflow stay the same. Keyboard selection preserves focus
on the existing button when its label changes.

These changes do not retire artifact files. The
[previous preview](FORGE_5112.md) describes durable helper summaries and browser
pane ownership; the [computer-tool guide](FORGE_5110.md) describes desktop limits.
