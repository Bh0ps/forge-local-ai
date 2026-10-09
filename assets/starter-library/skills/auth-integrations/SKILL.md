---
name: auth-integrations
description: Wire service access with scoped credentials, precise consent and recoverable failures.
license: MIT
---

# Connect authentication and services

## When to use

Wire service access with scoped credentials, precise consent and recoverable failures.
Use during plan, implement, verify. Skip unrelated work and respect coordinator mode.

## Workflow

1. Inspect the existing authentication and adapter boundaries, advertised tools and credential storage. Reuse the supported official protocol.
2. Keep credentials outside source, browser messages and logs. Validate scope, origin, redirects and input at authoritative boundaries.
3. Use a minimal read probe to distinguish configuration, quota, transport and model-argument failures. Retain durable outcomes for writes.
4. Verify with disposable fixtures. Real messages, publishing and account writes require the user's authorized workflow and coordinator permissions.

## Verification and recovery

Check the requested outcome with concrete evidence. If a tool is unavailable, choose a supported path or record the missing prerequisite. Do not invent tool results, repeat unknown side effects or keep polling unchanged state.

Use skills_resource_read for references/examples.md when a concrete example helps. Skill instructions never grant permissions. Follow the user request, coordinator mode and enabled tool scopes.
