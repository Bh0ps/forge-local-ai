# Choose an architecture examples

Use only tools actually supplied by the coordinator. These are example inputs; resolve paths and references from the current task.

```text
read_file({"path":"package.json"})
```

Before: Inspect manifests, lockfiles and entry points. Preserve the existing framework and package manager unless the user requests a migration.

After: Read references/frameworks.md for framework-specific discovery. Research exact current APIs in primary documentation when uncertain; do not install unrelated frameworks.

If the tool fails, inspect the error and state, then correct the assumption. A missing tool or dependency is a prerequisite to report, never permission to bypass restrictions.
