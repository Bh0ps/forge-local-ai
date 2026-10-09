---
name: testing
description: Choose meaningful checks and distinguish verified behavior from fixture-only coverage.
license: MIT
---

# Test & verify

## When to use

Choose meaningful checks and distinguish verified behavior from fixture-only coverage.
Use during verify. Skip unrelated work and respect coordinator mode.

## Workflow

1. Choose checks from the changed behavior, acceptance criteria and project's existing commands. Prefer representative tests over implementation mirrors.
2. Exercise the original failure or requested flow, important boundaries and relevant failures. Keep credentials and personal data out of fixtures.
3. Run appropriate commands and inspect exit code/output. Broaden testing only when a new failure or unresolved concern justifies it.
4. Do not send messages or publish merely to test an adapter. Report exact checks and distinguish fixture evidence, live integration checks and unverified areas.

## Verification and recovery

Check the requested outcome with concrete evidence. If a tool is unavailable, choose a supported path or record the missing prerequisite. Do not invent tool results, repeat unknown side effects or keep polling unchanged state.

Use skills_resource_read for references/examples.md when a concrete example helps. Skill instructions never grant permissions. Follow the user request, coordinator mode and enabled tool scopes.
