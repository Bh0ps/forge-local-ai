# Use tools efficiently examples

These examples use synthetic content. Resolve placeholder paths, hashes, IDs, revisions and selectors from current tool results before calling anything. Use only offered tools; examples grant no permissions.

## Read once, edit against the current hash

A synthetic form leaves its submit control disabled after failure. Locate the pending-state owner; the search returns component paths. Read only the matching component and caller. Each read supplies its current SHA-256.

Call `search_files`:

```json
{
  "query": "pending",
  "path": "src",
  "glob": "*.tsx"
}
```

Call `read_files`:

```json
{
  "files": [
    {
      "path": "<returned component path>",
      "start_line": 1,
      "end_line": 100
    },
    {
      "path": "<returned state-owner path>",
      "start_line": 80,
      "end_line": 180
    }
  ]
}
```

Expected evidence: Identify the missing failure transition at a returned file/line. Use apply_patch only after resolving the path and expected_sha256 from the current read; group related edits in its changes array. Re-read changed ranges and check the failure/retry behavior.

Recovery: If an edit reports a stale hash, no write occurred. Read the changed file, preserve the user's intervening work and create a corrected patch. Do not replace the hash without reconciling the contents.

## Wait for the existing check and read its output

A long check is started once using command_start with the repository's real argv, cwd and maximum duration. Retain the returned command session ID. Waiting may still report running; it is not a failure or a reason to launch the check again.

Call `command_wait`:

```json
{
  "id": "<returned command session id>",
  "timeout": 30
}
```

Call `command_read`:

```json
{
  "id": "<same returned command session id>",
  "start": 0,
  "limit": 4000
}
```

Call `artifact_read`:

```json
{
  "id": "<returned artifact id>",
  "start": 8000,
  "limit": 4000
}
```

Expected evidence: Use the completed session's exit status and relevant output. When a result points to an artifact, retrieve its missing range with artifact_read; start=8000 is appropriate only when that range actually exists. Cite the check and criterion it covers.

Recovery: Wait in bounded intervals while useful independent work continues. If the invocation has outcome_unknown, inspect durable invocation/session state and use the coordinator's recovery action; repeating a command could duplicate its effects.

## A permission denial is a recovery decision

An enabled tool may still be denied for this resource. Its denial explains whether the invocation was not_executed or uncertain.

Expected evidence: Retain the error category and show the supported approval/access action or missing prerequisite. Continue independent authorized work if the blocked action is optional.

Recovery: Do not substitute a shell, another account or a helper to evade access, consent, quota or authentication restrictions. Retry only after the relevant recovery changed the prerequisite.
