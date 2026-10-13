# Verify the live app examples

These examples use synthetic content. Resolve placeholder paths, hashes, IDs, revisions and selectors from current tool results before calling anything. Use only offered tools; examples grant no permissions.

## A form flow with fresh snapshots

The disposable app adds a note. Obtain the current preview ID and current Builder revision; IDs are distinct from browser tabs. Open the visible pane, inspect, type synthetic text, inspect again, then press Enter using the new snapshot and returned target. The example revision 3 must be replaced by builder_read's actual revision.

Call `preview_status`:

```json
{
  "id": "<returned preview id>"
}
```

Call `preview_open`:

```json
{
  "id": "<same preview id>"
}
```

Call `preview_inspect`:

```json
{
  "id": "<same preview id>"
}
```

Call `preview_type`:

```json
{
  "id": "<same preview id>",
  "selector": "<textbox selector returned by inspect>",
  "snapshot_id": "<current snapshot id>",
  "text": "Synthetic idea"
}
```

Call `preview_inspect`:

```json
{
  "id": "<same preview id>"
}
```

Call `preview_key`:

```json
{
  "id": "<same preview id>",
  "selector": "<fresh textbox selector>",
  "snapshot_id": "<fresh snapshot id>",
  "key": "Enter"
}
```

Call `preview_diagnostics`:

```json
{
  "id": "<same preview id>"
}
```

Call `builder_record_check`:

```json
{
  "id": "<current Builder id>",
  "expected_revision": 3,
  "gate": "functional",
  "status": "passed",
  "source": "session",
  "reference": "<completed test session id>",
  "note": "The disposable add-item test passed, including Enter and focus return."
}
```

Expected evidence: Observe that the note appears once, the input clears and focus returns. Inspect after Enter before further interactions. Test empty/invalid input and a controlled failure/retry where supported. The last builder_record_check call is conditional: use only a real completed approved test session that proved this flow, never a made-up reference or screenshot.

Recovery: If a snapshot is stale, inspect again. After a dispatched action times out, inspect outcome before retrying. Separate expected negative-test responses and earlier diagnostics from a new defect; never discard current runtime errors merely to obtain a passed gate.

## Generated reports versus changed source

A completed approved test command writes JUnit, coverage or TypeScript cache outputs. The coordinator owns its launch/completion/evidence manifests and exact generated-output classification.

Expected evidence: Use the completed session reference and current revision when recording the check. A recognized report does not stale unchanged source. If real source changes after the command, rerun the relevant check before recording evidence; older sessions without a launch snapshot need a fresh check.

Recovery: Do not edit source hashes or broaden ignores to all XML/JSON or all new files. A stale evidence rejection is a request for current verification, not permission to bypass the coordinator's manifest.

## A screenshot requires a visible preview pane

The capture reports that the native preview is hidden or outside supported bounds. The managed preview may still be serving correctly.

Expected evidence: Call preview_open with the current preview ID, confirm readiness and inspect the visible result before capture. Set only a supported viewport within the pane; inspect wide and narrow layouts at the sizes actually available.

Recovery: Never relax native bounds, use a browser tab ID as a preview ID or bypass snapshot guards. If a required viewport or image review is unavailable, state the precise unchecked surface and preserve other observed evidence.
