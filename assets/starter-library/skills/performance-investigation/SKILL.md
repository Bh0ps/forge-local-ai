---
name: performance-investigation
description: Measure representative bottlenecks and improve elapsed task time without losing correctness.
license: MIT
---

# Measure and improve performance

## When to use

Measure representative bottlenecks and improve elapsed task time without losing correctness.
Use during discover, implement, verify. Skip unrelated work and respect coordinator mode.

## Workflow

1. Define the representative task and baseline. Separate queue, model loading, prompt processing, generation, tool and UI time.
2. Profile or instrument the suspected bottleneck. Compare equivalent workloads, model/context settings and warm/cold conditions.
3. Apply one evidence-based change at the responsible boundary. Preserve explicit model choices and avoid claiming hardware-independent speedups.
4. Compare elapsed time, prompt tokens, redundant tools, memory and task correctness. Report measured gains and unresolved limits, not just tokens per second.

## Verification and recovery

Check the requested outcome with concrete evidence. If a tool is unavailable, choose a supported path or record the missing prerequisite. Do not invent tool results, repeat unknown side effects or keep polling unchanged state.

Use skills_resource_read for references/examples.md when a concrete example helps. Skill instructions never grant permissions. Follow the user request, coordinator mode and enabled tool scopes.
