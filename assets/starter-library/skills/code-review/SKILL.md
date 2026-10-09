---
name: code-review
description: Find actionable correctness, security and recovery defects with precise evidence.
license: MIT
---

# Review code

## When to use

Find actionable correctness, security and recovery defects with precise evidence.
Use during verify. Skip unrelated work and respect coordinator mode.

## Workflow

1. Read the request, applicable guidance and diff. Trace important behavior through callers, persistence, validation, permissions and failure paths.
2. Prioritize concrete trigger/consequence pairs. Use relevant tests or a small reproduction to resolve uncertain claims.
3. For each finding provide severity, exact file location and a concise explanation. Avoid speculative style objections.
4. Treat helper observations as evidence to verify. Preserve the user's work and distinguish review findings from implemented fixes.

## Verification and recovery

Check the requested outcome with concrete evidence. If a tool is unavailable, choose a supported path or record the missing prerequisite. Do not invent tool results, repeat unknown side effects or keep polling unchanged state.

Use skills_resource_read for references/examples.md when a concrete example helps. Skill instructions never grant permissions. Follow the user request, coordinator mode and enabled tool scopes.
