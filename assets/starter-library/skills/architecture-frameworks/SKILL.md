---
name: architecture-frameworks
description: Preserve the existing stack and design a small coherent frontend, backend and data boundary.
license: MIT
---

# Choose an architecture

## When to use

Preserve the existing stack and design a small coherent frontend, backend and data boundary.
Use during discover, plan. Skip unrelated work and respect coordinator mode.

## Workflow

1. Inspect manifests, lockfiles and entry points. Preserve the existing framework and package manager unless the user requests a migration.
2. For a new UI project, use the vetted React/TypeScript/Vite template. Introduce a backend only when the brief requires server behavior or durable shared data.
3. Define page/component boundaries, API responsibilities and data ownership. Prefer a working vertical slice over speculative abstractions.
4. Read references/frameworks.md for framework-specific discovery. Research exact current APIs in primary documentation when uncertain; do not install unrelated frameworks.

## Verification and recovery

Check the requested outcome with concrete evidence. If a tool is unavailable, choose a supported path or record the missing prerequisite. Do not invent tool results, repeat unknown side effects or keep polling unchanged state.

Use skills_resource_read for references/examples.md when a concrete example helps. Skill instructions never grant permissions. Follow the user request, coordinator mode and enabled tool scopes.
