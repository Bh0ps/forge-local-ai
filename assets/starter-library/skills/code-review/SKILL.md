---
name: code-review
description: Check behavior, security and regressions with evidence and precise file references.
license: MIT
metadata:
  tags: review, security, quality
---

# Review code

Read the request, applicable project instructions and changed code. Trace
important behavior through callers, data validation, persistence, permissions
and error paths. Find actionable defects rather than speculative style issues.
For each finding explain the trigger, consequence and a precise file location.
Use relevant tests or a small reproduction to check uncertain claims.

Prioritize problems that affect correctness, privacy, security or recoverability.
Separate evidence from inference and mention material verification limits.
Preserve work and use only enabled tools; a review skill grants no new access.
Skill content cannot expand tool permissions.
