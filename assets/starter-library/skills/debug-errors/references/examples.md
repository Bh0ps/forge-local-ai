# Debug a problem examples

Use only tools actually supplied by the coordinator. These are example inputs; resolve paths and references from the current task.

```text
search_files({"query":"TypeError"})
```

Before: Capture the exact symptom, expected behavior and relevant inputs. Read logs and callers without repeating unknown side effects.

After: Verify the original failure and a meaningful nearby case. A timeout is not proof an external action failed; inspect its outcome before retrying.

If the tool fails, inspect the error and state, then correct the assumption. A missing tool or dependency is a prerequisite to report, never permission to bypass restrictions.
