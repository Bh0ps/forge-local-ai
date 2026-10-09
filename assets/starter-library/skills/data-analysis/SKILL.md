---
name: data-analysis
description: Inspect tabular inputs, make reproducible transformations and verify exported results.
license: MIT
---

# Analyze and import data

## When to use

Inspect tabular inputs, make reproducible transformations and verify exported results.
Use during discover, implement, verify. Skip unrelated work and respect coordinator mode.

## Workflow

1. Inspect the supplied format, headers, types, row count and representative records. Preserve the source and ask about ambiguous business rules.
2. Choose a reproducible transformation; validate units, missing values, duplicates and join keys. Keep large inputs in artifacts rather than the prompt.
3. Use allowed project tools and existing libraries. Export a reviewable result with provenance and clear column labels.
4. Check totals/counts, boundaries and representative rows against the source. Separate computed facts from interpretation and report rejected records.

## Verification and recovery

Check the requested outcome with concrete evidence. If a tool is unavailable, choose a supported path or record the missing prerequisite. Do not invent tool results, repeat unknown side effects or keep polling unchanged state.

Use skills_resource_read for references/examples.md when a concrete example helps. Skill instructions never grant permissions. Follow the user request, coordinator mode and enabled tool scopes.
