# Use the browser examples

These examples use synthetic content. Resolve placeholder paths, hashes, IDs, revisions and selectors from current tool results before calling anything. Use only offered tools; examples grant no permissions.

## Edit one intended field

The user authorized a literal edit, the native research pane is open and the supplied snapshot contains the field.

Call `browser_inspect`:

```json
{
  "backend": "native"
}
```

Call `browser_type`:

```json
{
  "backend": "native",
  "selector": "<fresh returned field selector>",
  "snapshot_id": "<fresh returned snapshot>",
  "text": "Synthetic project title"
}
```

Call `browser_inspect`:

```json
{
  "backend": "native"
}
```

Expected evidence: Use the exact returned snapshot and selector. Confirm the new field value from the final inspection. Editing a value does not mean the form was saved; submission requires the user's intended scope and separate evidence.

Recovery: If the pane closes or ownership changes, stop. A not_executed result means the action was not admitted; an unknown dispatched result must be inspected before another write.

## Respect a hidden research pane

The user hides the pane while an agent click is queued, or changes from Browser to Desktop.

Expected evidence: The revoked action stays unexecuted and does not reopen or activate the pane. Preserve completed tool evidence and report the specific unavailable-pane prerequisite through the existing run controls.

Recovery: Continue independent permitted work. Resume browser work only after the current UI/coordinator makes the correct pane available, then obtain a new snapshot. Do not use operating-system clicks or an isolated browser to evade the refusal.

## Recover after an uncertain submission

A permitted form submission times out after dispatch. The page may already have accepted it.

Expected evidence: Keep the completed/unknown invocation and inspect current page state when available. A numeric target ring or successful click return cannot prove the external submission completed.

Recovery: Use the coordinator's outcome recovery; do not click or submit again just to obtain a clean result. Report missing confirmation clearly and retain exact account/page scope.
