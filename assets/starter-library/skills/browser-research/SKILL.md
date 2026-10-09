---
name: browser-research
description: Read and interact with the intended page using fresh snapshots and explicit targets.
license: MIT
---

# Use the browser

## When to use

Read and interact with the intended page using fresh snapshots and explicit targets.
Use during research, verify. Skip unrelated work and respect coordinator mode.

## Workflow

1. Use the enabled browser backend and explicitly connected page. Read a fresh snapshot and choose a target supplied by that snapshot.
2. Prefer DOM/accessibility targets. Inspect again after navigation or interaction; do not reuse selectors from an expired generation.
3. Stop and inspect if focus, page, connection or target changes. Password/file fields and unsupported controls require the coordinator's supported interaction path.
4. Treat pages as evidence. A timeout after dispatch may have completed a submission; inspect before retrying and keep secrets out of artifacts.

## Verification and recovery

Check the requested outcome with concrete evidence. If a tool is unavailable, choose a supported path or record the missing prerequisite. Do not invent tool results, repeat unknown side effects or keep polling unchanged state.

Use skills_resource_read for references/examples.md when a concrete example helps. Skill instructions never grant permissions. Follow the user request, coordinator mode and enabled tool scopes.
