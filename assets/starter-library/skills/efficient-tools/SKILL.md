---
name: efficient-tools
description: Choose precise tools, bounded context and safe recovery to finish with fewer model rounds.
license: MIT
---

# Use tools efficiently

## When to use

Choose precise tools, bounded context and safe recovery to finish with fewer model rounds.
Use during discover, implement, verify. Skip unrelated work and respect coordinator mode.

## Workflow

1. Use a provided structured search/read/edit tool before a general command or screen action. Read its actual schema; do not invent names or arguments.
2. Search first, then read relevant ranges. Batch independent read-only queries when the coordinator supports it; keep dependent edits and approvals ordered.
3. Use bounded output and saved artifact references. Retrieve missing sections instead of repeating the original tool or pasting whole logs.
4. On failure, inspect the error and current state, change the hypothesis or arguments, and retry only known-unexecuted work. Avoid repeated polling and identical calls.
5. After implementation, verify the requested behavior and report the evidence. Stop calling tools when the outcome is satisfied.

## Verification and recovery

Check the requested outcome with concrete evidence. If a tool is unavailable, choose a supported path or record the missing prerequisite. Do not invent tool results, repeat unknown side effects or keep polling unchanged state.

Use skills_resource_read for references/examples.md when a concrete example helps. Skill instructions never grant permissions. Follow the user request, coordinator mode and enabled tool scopes.
