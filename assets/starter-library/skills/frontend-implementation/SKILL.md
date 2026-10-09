---
name: frontend-implementation
description: Implement working pages and components with real interaction, coherent state and the existing framework.
license: MIT
---

# Build the frontend

## When to use

Implement working pages and components with real interaction, coherent state and the existing framework.
Use during implement. Skip unrelated work and respect coordinator mode.

## Workflow

1. Read the related component, styles, state owner and callers. Preserve framework conventions and existing user work.
2. Implement the smallest complete user flow. Keep state in an appropriate owner, use stable keys and handle loading/error/empty cases.
3. Connect controls to real behavior. Avoid decorative buttons, invented network results and hard-coded success paths.
4. Run relevant type/build checks, open the managed preview and exercise the main flow. Inspect runtime errors before proceeding to visual refinement.

## Verification and recovery

Check the requested outcome with concrete evidence. If a tool is unavailable, choose a supported path or record the missing prerequisite. Do not invent tool results, repeat unknown side effects or keep polling unchanged state.

Use skills_resource_read for references/examples.md when a concrete example helps. Skill instructions never grant permissions. Follow the user request, coordinator mode and enabled tool scopes.
