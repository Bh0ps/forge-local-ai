# Git & worktrees examples

Use only tools actually supplied by the coordinator. These are example inputs; resolve paths and references from the current task.

```text
run_command({"argv":["git","status","--short"],"timeout":60})
```

Before: Inspect status, intended branch and relevant diff before changing Git state. Preserve dirty work.

After: Publishing, merging and destructive operations follow the user request and approval policy. Keep credentials and personal state out of Git.

If the tool fails, inspect the error and state, then correct the assumption. A missing tool or dependency is a prerequisite to report, never permission to bypass restrictions.
