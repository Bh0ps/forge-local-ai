# Build the frontend examples

Use only tools actually supplied by the coordinator. These are example inputs; resolve paths and references from the current task.

```text
read_file({"path":"src/App.tsx"})
```

Before: Read the related component, styles, state owner and callers. Preserve framework conventions and existing user work.

After: Run relevant type/build checks, open the managed preview and exercise the main flow. Inspect runtime errors before proceeding to visual refinement.

If the tool fails, inspect the error and state, then correct the assumption. A missing tool or dependency is a prerequisite to report, never permission to bypass restrictions.
