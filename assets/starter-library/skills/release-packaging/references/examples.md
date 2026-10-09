# Verify a release examples

Use only tools actually supplied by the coordinator. These are example inputs; resolve paths and references from the current task.

```text
read_file({"path":"build.ps1"})
```

Before: Inspect the project's actual build and release scripts, dependency locks and licensing requirements. Record the intended platform.

After: Report actual checks and unresolved platform/signing limits. Publishing or deployment requires the user's authorized workflow.

If the tool fails, inspect the error and state, then correct the assumption. A missing tool or dependency is a prerequisite to report, never permission to bypass restrictions.
