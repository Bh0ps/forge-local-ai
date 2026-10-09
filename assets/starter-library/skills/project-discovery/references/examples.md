# Explore a project examples

Use only tools actually supplied by the coordinator. These are example inputs; resolve paths and references from the current task.

```text
search_files({"query":"login","path":"src"})
```

Before: Read the current request and coordinator-provided project guidance. Inspect the relevant file layout and manifests with bounded tools.

After: Report confirmed conventions and concrete gaps. Ask only for product decisions that the repository cannot answer.

If the tool fails, inspect the error and state, then correct the assumption. A missing tool or dependency is a prerequisite to report, never permission to bypass restrictions.
