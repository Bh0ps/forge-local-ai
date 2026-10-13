# Build the frontend examples

These examples use synthetic content. Resolve placeholder paths, hashes, IDs, revisions and selectors from current tool results before calling anything. Use only offered tools; examples grant no permissions.

## Closing a form during a delayed save

Locate a synthetic form's submit handler and state owner. Read the component, caller and actual package scripts before changing code. Reuse existing controls and styles.

Call `search_files`:

```json
{
  "query": "submit",
  "path": "src",
  "glob": "*.tsx"
}
```

Call `read_files`:

```json
{
  "files": [
    {
      "path": "<form component>",
      "start_line": 1,
      "end_line": 150
    },
    {
      "path": "<caller>",
      "start_line": 1,
      "end_line": 100
    },
    {
      "path": "package.json",
      "start_line": 1,
      "end_line": 80
    }
  ]
}
```

Call `preview_inspect`:

```json
{
  "id": "<current preview id>"
}
```

Expected evidence: Reproduce: open the form, edit its draft, submit, close before the response resolves, then resolve it. The dialog stays closed; stale replies cannot replace a newer draft or selection. Reopening uses the intended saved state. Verify success, visible error and a real retry, including keyboard focus return.

Recovery: Bind the result to the current entity/request generation and invalidate it on close/unmount. Cancellation alone may not stop a late reply. Preserve the draft on a failed save, and show a retry that calls the actual operation.

## Switching entities during metadata loading

Start loading entity A, select B, resolve B and then A. Also close the editor during either request and test a failed load followed by retry.

Expected evidence: Only B's data updates the visible B editor; a dismissed editor stays closed. Loading/error states identify the current entity and retry preserves user-entered values. Cover the race with controlled promises rather than elapsed-time sleeps.

Recovery: Reject responses whose entity or generation no longer matches. Scope refresh callbacks to the active editor and avoid capturing a stale selected ID. Do not silence errors to disguise a missing lifecycle guard.

## Check the real interaction and retained visual identity

Use the managed current preview after relevant repository checks. Exercise the same synthetic success/error/retry flow with keyboard controls at wide and narrow widths, enlarged text and supported light/dark themes.

Expected evidence: Confirm readable labels, visible focus, no clipped controls and preserved palette/type/icon conventions. Verify reduced-motion and hidden-app handling for continuous accents. Keep screenshot evidence after the final relevant edit; image review is required for appearance claims.

Recovery: A passing build proves compilation. Record any unavailable failure branch or image review explicitly. Fix the observed component/token issue without replacing the framework or adding decorative controls.
