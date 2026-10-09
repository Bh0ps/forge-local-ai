# Plan & build examples

Use only tools actually supplied by the coordinator. These are example inputs; resolve paths and references from the current task.

```text
request_user_input({"questions":[{"id":"storage","header":"Storage","question":"Should records persist after restart?","options":[{"label":"Local database","description":"Keeps records on this computer.","recommended":true},{"label":"Session only","description":"Simpler prototype with temporary records."}]}]})
```

Before: Inspect the existing implementation. Capture the user outcome, audience, affected flows and measurable acceptance criteria.

After: Complete a working vertical slice, verify its acceptance criteria, record evidence and continue authorized work. Do not reinterpret a started goal as another request to plan.

If the tool fails, inspect the error and state, then correct the assumption. A missing tool or dependency is a prerequisite to report, never permission to bypass restrictions.
