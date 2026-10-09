# Analyze and import data examples

Use only tools actually supplied by the coordinator. These are example inputs; resolve paths and references from the current task.

```text
read_file({"path":"data/sample.csv","start_line":1,"end_line":40})
```

Before: Inspect the supplied format, headers, types, row count and representative records. Preserve the source and ask about ambiguous business rules.

After: Check totals/counts, boundaries and representative rows against the source. Separate computed facts from interpretation and report rejected records.

If the tool fails, inspect the error and state, then correct the assumption. A missing tool or dependency is a prerequisite to report, never permission to bypass restrictions.
