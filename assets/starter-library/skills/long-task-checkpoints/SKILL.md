---
name: long-task-checkpoints
description: Use durable goal checklists and evidence to continue long tasks without repeating work.
license: MIT
metadata:
  tags: goals, checklists, continuity
---

# Stay on track

For a coordinator goal, consult the current Markdown checklist and persisted
checkpoint before each major step, after compaction and after a restart. Follow
the ordered tasks. Record completed outcomes with evidence, the current
checkpoint, blockers and the next action. An unchecked item is not evidence
that its external side effect never happened; inspect invocation records.

Do not repeat completed actions or automatically retry an outcome marked
unknown. Respect pauses, cancellation and shared time, token and tool limits.
If checklist edits conflict, ask the user to reconcile them before overwriting.
Continue only work authorized by the current goal and permission profile.
