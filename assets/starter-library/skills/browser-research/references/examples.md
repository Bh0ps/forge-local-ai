# Use the browser examples

Use only tools actually supplied by the coordinator. These are example inputs; resolve paths and references from the current task.

```text
browser_inspect({})
```

Before: Use the enabled browser backend and explicitly connected page. Read a fresh snapshot and choose a target supplied by that snapshot.

After: Treat pages as evidence. A timeout after dispatch may have completed a submission; inspect before retrying and keep secrets out of artifacts.

If the tool fails, inspect the error and state, then correct the assumption. A missing tool or dependency is a prerequisite to report, never permission to bypass restrictions.
