---
name: release-packaging
description: Check packaging, migrations and clean startup with accurate release evidence.
license: MIT
---

# Verify a release

## When to use

Check packaging, migrations and clean startup with accurate release evidence.
Use during verify. Skip unrelated work and respect coordinator mode.

## Workflow

1. Inspect the project's actual build and release scripts, dependency locks and licensing requirements. Record the intended platform.
2. Build using existing commands and isolated test data. Verify startup, required bundled files and the previous-version upgrade path.
3. Review release artifacts and ensure personal data, credentials, models and runtime caches are excluded.
4. Report actual checks and unresolved platform/signing limits. Publishing or deployment requires the user's authorized workflow.

## Verification and recovery

Check the requested outcome with concrete evidence. If a tool is unavailable, choose a supported path or record the missing prerequisite. Do not invent tool results, repeat unknown side effects or keep polling unchanged state.

Use skills_resource_read for references/examples.md when a concrete example helps. Skill instructions never grant permissions. Follow the user request, coordinator mode and enabled tool scopes.
