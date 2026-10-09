# Test & verify examples

Use only tools actually supplied by the coordinator. These are example inputs; resolve paths and references from the current task.

```text
run_command({"argv":["python","-m","pytest","-q"],"cwd":".","timeout":60})
```

Before: Choose checks from the changed behavior, acceptance criteria and project's existing commands. Prefer representative tests over implementation mirrors.

After: Do not send messages or publish merely to test an adapter. Report exact checks and distinguish fixture evidence, live integration checks and unverified areas.

If the tool fails, inspect the error and state, then correct the assumption. A missing tool or dependency is a prerequisite to report, never permission to bypass restrictions.
