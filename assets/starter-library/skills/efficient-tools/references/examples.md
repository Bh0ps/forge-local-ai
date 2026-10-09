# Use tools efficiently examples

Use only tools actually supplied by the coordinator. These are example inputs; resolve paths and references from the current task.

```text
artifact_read({"id":"<artifact reference from the tool result>","start":8000,"limit":4000})
```

Before: Use a provided structured search/read/edit tool before a general command or screen action. Read its actual schema; do not invent names or arguments.

After: After implementation, verify the requested behavior and report the evidence. Stop calling tools when the outcome is satisfied.

If the tool fails, inspect the error and state, then correct the assumption. A missing tool or dependency is a prerequisite to report, never permission to bypass restrictions.
