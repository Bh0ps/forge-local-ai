# Build backend APIs examples

Use only tools actually supplied by the coordinator. These are example inputs; resolve paths and references from the current task.

```text
search_files({"query":"route"})
```

Before: Inspect existing routes, callers and persistence. Define request/response behavior and authorization at the service boundary.

After: Test the normal case, invalid input and one meaningful failure boundary with disposable data. Verify compatibility for existing callers.

If the tool fails, inspect the error and state, then correct the assumption. A missing tool or dependency is a prerequisite to report, never permission to bypass restrictions.
