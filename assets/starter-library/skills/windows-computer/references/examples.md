# Work with Windows controls examples

These examples use synthetic content. Resolve placeholder paths, hashes, IDs, revisions and selectors from current tool results before calling anything. Use only offered tools; examples grant no permissions.

## Edit a supported background control

The user authorized a literal edit in a known app while working elsewhere. Exact tools are enabled and the app permission is granted.

Call `computer_windows`:

```json
{}
```

Call `computer_inspect`:

```json
{
  "window_id": 101,
  "include_image": false
}
```

Call `computer_set_value`:

```json
{
  "snapshot_id": "returned_snapshot_id",
  "element_id": 1,
  "value": "Synthetic literal {braces} Ω"
}
```

Expected evidence: Replace example IDs with fresh returned identities. Confirm ok:true, then inspect again to verify the actual value. Distinguish the broker's pointer-free request from the app's own behavior.

Recovery: If ValuePattern is unsupported, read-only or refused, report that precise limitation. Obtain any genuinely missing user scope before a different input method; guidance does not authorize it.

## Recover from uncertain input

An approved action times out after dispatch, or its snapshot has been consumed.

Expected evidence: Record the unknown outcome and completed invocation. A new inspection determines the next necessary action; it does not justify repeating the original input.

Recovery: Stop if Windows is locked, cancellation is active or the target identity changed. Resume only through the coordinator's recovery path. Never substitute another app or reuse old coordinates.
