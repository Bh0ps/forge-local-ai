---
name: frontend-implementation
description: Implement working pages and components with real interaction, coherent state and the existing framework.
license: MIT
---

# Build the frontend

## When to use

Use during implement. Skip unrelated work and respect coordinator mode.

## Workflow

1. Read component, caller, state owner, styles/tokens and check scripts. Preserve framework, dependencies and visual identity.
2. Build one complete flow with existing controls/icons. Implement real loading, empty, error, success and retry states; never invent server results.
3. Bind replies to current entity/request generation; invalidate on close, scope change and unmount. Preserve drafts, selections and reading intent.
4. Use native semantics, stable IDs and accessible names. Verify open/close/Escape focus, double-submit, delayed replies and cancellation.
5. Reuse spacing/type/palettes. Check narrow layouts, enlarged text and supported themes. Stop continuous motion while hidden; remove it for reduced motion. Never animate streaming characters.
6. Run relevant repository checks, then exercise the real preview flow and failure/retry path. Fix observed defects before recording evidence.

## Verification and recovery

Demonstrate the user outcome, keyboard path and one asynchronous/failure boundary; types/build only prove compilation. Repair lifecycle guards rather than hiding errors; preserve checks and use actual recovery for failed/unknown actions.

## Result packet

Before/after behavior; components/state owner; tested states/keyboard/races; check evidence; unverified surfaces.

Read references/examples.md with skills_resource_read for examples. Follow coordinator mode and enabled scopes; skills grant no permissions.
