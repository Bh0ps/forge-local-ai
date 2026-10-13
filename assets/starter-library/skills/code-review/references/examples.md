# Review code examples

These examples use synthetic content. Resolve placeholder paths, hashes, IDs, revisions and selectors from current tool results before calling anything. Use only offered tools; examples grant no permissions.

## Stopping a monitor during a blocked read

Read the changed monitor, caller and admission boundary; use a synthetic event-controlled read that pauses before final worker admission. Stop the monitor, release the read and inspect whether a worker can still start.

Call `search_files`:

```json
{
  "query": "request_generation",
  "path": "src"
}
```

Call `read_files`:

```json
{
  "files": [
    {
      "path": "<changed implementation>",
      "start_line": 1,
      "end_line": 150
    },
    {
      "path": "<caller or persistence boundary>",
      "start_line": 1,
      "end_line": 150
    }
  ]
}
```

Call `artifact_read`:

```json
{
  "id": "<returned reproduction/test artifact>",
  "start": 0,
  "limit": 4000
}
```

Expected evidence: A finding states exact file/line, stop/read/admit trigger, observed late work, smallest lifecycle guard and a deterministic regression. Verify stop remains bounded if I/O stalls, and that cancelling the old background operation does not cancel a newer manual operation.

Recovery: A passing happy-path test does not resolve the race. Prefer synchronization events over timing sleeps. If only code suggests the defect and no reproduction was run, label it as an inference and explain the responsible boundary.

## A stale tool descriptor after account changes

An enabled MCP tool was discovered under synthetic account A. Pause before SDK admission, edit headers/env/credential references or disable the tool/server, then release the request.

Expected evidence: Verify a stale descriptor cannot run under account B, current enabled state is checked at admission, cached SDK configuration is an immutable binding and the request claim is released. Test pre-ready failure as not_executed, and an error after dispatch as outcome_unknown. Error reports retain sanitized categories, not credentials.

Recovery: Use an in-memory vault and dummy connection, never a real account. Do not hold a broad hub lock across network readiness. Unknown dispatched writes need outcome recovery before replay; configuration changes must not silently reinterpret a pending action.

## A delayed response reopening a dismissed editor

Review the dialog's close/unmount generation and callbacks, then control a pending synthetic metadata response. Close the editor or switch entities before resolving the response.

Expected evidence: The dialog remains dismissed and the active entity's draft is unchanged. Check loading/error/retry and keyboard close/focus behavior. Report user-visible consequences with source locations, separating reproduced defects from visual preferences.

Recovery: Verify the repaired failure boundary and a nearby success case. A helper's claim needs local confirmation; if no actionable defect is found, report coverage and limits without claiming the application is defect-free.
