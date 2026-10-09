---
name: debug-errors
description: Reproduce a failure, verify its cause and check the smallest reliable fix.
license: MIT
---

# Debug a problem

## When to use

Reproduce a failure, verify its cause and check the smallest reliable fix.
Use during discover, implement, verify. Skip unrelated work and respect coordinator mode.

## Workflow

1. Capture the exact symptom, expected behavior and relevant inputs. Read logs and callers without repeating unknown side effects.
2. Form one testable hypothesis and reproduce in a disposable fixture. Trace the responsible boundary instead of applying speculative changes.
3. Fix the cause while preserving existing work. When a probe disproves the hypothesis, update it rather than repeating the same tool call.
4. Verify the original failure and a meaningful nearby case. A timeout is not proof an external action failed; inspect its outcome before retrying.

## Verification and recovery

Check the requested outcome with concrete evidence. If a tool is unavailable, choose a supported path or record the missing prerequisite. Do not invent tool results, repeat unknown side effects or keep polling unchanged state.

Use skills_resource_read for references/examples.md when a concrete example helps. Skill instructions never grant permissions. Follow the user request, coordinator mode and enabled tool scopes.
