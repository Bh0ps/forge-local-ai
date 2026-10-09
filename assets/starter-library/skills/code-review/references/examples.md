# Review code examples

Use only tools actually supplied by the coordinator. These are example inputs; resolve paths and references from the current task.

```text
read_file({"path":"src/service.ts"})
```

Before: Read the request, applicable guidance and diff. Trace important behavior through callers, persistence, validation, permissions and failure paths.

After: Treat helper observations as evidence to verify. Preserve the user's work and distinguish review findings from implemented fixes.

If the tool fails, inspect the error and state, then correct the assumption. A missing tool or dependency is a prerequisite to report, never permission to bypass restrictions.
