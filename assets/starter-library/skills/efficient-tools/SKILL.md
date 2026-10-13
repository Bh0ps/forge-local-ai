---
name: efficient-tools
description: Choose precise tools, bounded context and safe recovery to finish with fewer model rounds.
license: MIT
---

# Use tools efficiently

## When to use

Use during discover, implement, verify. Skip unrelated work and respect coordinator mode.

## Workflow

1. Name the next question or acceptance check. If its tool is absent, tools_search then tools_load only exact returned enabled names.
2. Search first; read the matching caller, implementation and state owner with bounded read_files ranges. Reuse returned hashes and artifacts.
3. Batch independent reads when offered; order dependent edits. Use apply_patch with current expected_sha256; reread and reconcile stale hashes.
4. For long checks use command_start, command_wait and bounded command_read. Never restart a running process to obtain its output.
5. Classify failures: not_executed permits correction; completed retains evidence; outcome_unknown needs inspection and coordinator recovery. Authentication, quota, consent and access denial have specific paths.
6. Check the changed behavior, record its evidence and continue the next unmet criterion. Stop when the authorized outcome is satisfied.

## Verification and recovery

Link conclusions to returned paths, hashes, artifacts or completed command results and their acceptance criteria. Change arguments or hypotheses for unexecuted failures; preserve completed actions. Never replay uncertain commands or writes.

## Result packet

Outcome; changed paths; criterion -> observed result/evidence; unresolved limit; next action if blocked.

Read references/examples.md with skills_resource_read for examples. Follow coordinator mode and enabled scopes; skills grant no permissions.
