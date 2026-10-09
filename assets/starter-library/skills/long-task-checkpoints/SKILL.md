---
name: long-task-checkpoints
description: Continue durable tasks from evidence and checkpoints without repeating completed side effects.
license: MIT
---

# Stay on track

## When to use

Continue durable tasks from evidence and checkpoints without repeating completed side effects.
Use during implement, verify. Skip unrelated work and respect coordinator mode.

## Workflow

1. Read the saved goal, checkpoint and invocation outcomes before a major step, after compaction and after restart.
2. Follow the current work packet and ordered checklist. In guided execution, use the registered checks: the coordinator records their supported passing evidence and task progress. Do not add a bookkeeping round after every read or write. If a necessary check lacks a registered contract, retain its concrete artifact and update only the existing task ID with the current goal revision; do not replace the checklist or infer success from an answer or file existence.
3. An unchecked item or timeout does not prove that its effect never happened. Inspect an unknown invocation before retrying it.
4. Respect pause, cancellation and shared budgets. Reconcile an externally edited checklist before overwriting it; preserve the next action when blocked. Original requirements and independent review remain completion gates. Legacy runs may use goal_update for checkpoint notes while preserving supplied task IDs.

## Verification and recovery

Check the requested outcome with concrete evidence. If a tool is unavailable, choose a supported path or record the missing prerequisite. Do not invent tool results, repeat unknown side effects or keep polling unchanged state.

Use skills_resource_read for references/examples.md when a concrete example helps. Skill instructions never grant permissions. Follow the user request, coordinator mode and enabled tool scopes.
