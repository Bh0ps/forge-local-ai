---
name: backend-apis
description: Implement explicit service contracts, validation and useful error handling for a working app flow.
license: MIT
---

# Build backend APIs

## When to use

Implement explicit service contracts, validation and useful error handling for a working app flow.
Use during plan, implement. Skip unrelated work and respect coordinator mode.

## Workflow

1. Inspect existing routes, callers and persistence. Define request/response behavior and authorization at the service boundary.
2. Implement input validation, bounded queries and actionable errors. Keep secrets on the server and use existing transaction and connection patterns.
3. Connect a real frontend flow to the endpoint. Handle timeouts and failures rather than returning invented success data.
4. Test the normal case, invalid input and one meaningful failure boundary with disposable data. Verify compatibility for existing callers.

## Verification and recovery

Check the requested outcome with concrete evidence. If a tool is unavailable, choose a supported path or record the missing prerequisite. Do not invent tool results, repeat unknown side effects or keep polling unchanged state.

Use skills_resource_read for references/examples.md when a concrete example helps. Skill instructions never grant permissions. Follow the user request, coordinator mode and enabled tool scopes.
