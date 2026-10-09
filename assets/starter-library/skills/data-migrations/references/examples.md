# Model data and migrations examples

Use only tools actually supplied by the coordinator. These are example inputs; resolve paths and references from the current task.

```text
read_file({"path":"migrations/README.md"})
```

Before: Inspect the actual schema, migration sequence and transaction helpers. Identify ownership, required constraints and old-version compatibility.

After: Test a fresh database, upgrade from the previous schema and rollback/recovery behavior using isolated fixtures. Report limitations before destructive migration.

If the tool fails, inspect the error and state, then correct the assumption. A missing tool or dependency is a prerequisite to report, never permission to bypass restrictions.
