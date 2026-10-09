---
name: data-migrations
description: Design durable data with scoped queries, reversible changes and migration evidence.
license: MIT
---

# Model data and migrations

## When to use

Design durable data with scoped queries, reversible changes and migration evidence.
Use during plan, implement, verify. Skip unrelated work and respect coordinator mode.

## Workflow

1. Inspect the actual schema, migration sequence and transaction helpers. Identify ownership, required constraints and old-version compatibility.
2. Add the smallest required model and migration. Preserve IDs and personal data; use a consistent backup when a migration needs it.
3. Validate data at the application boundary and constraints in storage. Parameterize queries and keep concurrent updates transactional.
4. Test a fresh database, upgrade from the previous schema and rollback/recovery behavior using isolated fixtures. Report limitations before destructive migration.

## Verification and recovery

Check the requested outcome with concrete evidence. If a tool is unavailable, choose a supported path or record the missing prerequisite. Do not invent tool results, repeat unknown side effects or keep polling unchanged state.

Use skills_resource_read for references/examples.md when a concrete example helps. Skill instructions never grant permissions. Follow the user request, coordinator mode and enabled tool scopes.
