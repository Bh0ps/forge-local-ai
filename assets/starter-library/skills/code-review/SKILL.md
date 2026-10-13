---
name: code-review
description: Find actionable correctness, security and recovery defects with precise evidence.
license: MIT
---

# Review code

## When to use

Use during verify. Skip unrelated work and respect coordinator mode.

## Workflow

1. Read the requested outcome, guidance and focused diff. Trace callers, inputs, state ownership, validation, permissions and durable outcomes.
2. Prioritize concrete defects: stale replies, wrong entity/account, terminal races, interrupted actions, missing recovery, broken flows and credential exposure.
3. Inspect success/failure and relevant restart/cancellation boundaries. Reproduce uncertain concurrency with isolated synthetic fixtures; avoid personal data/accounts.
4. Give each finding severity, file/line, trigger, consequence, correction and regression. Distinguish reproduction from source-supported inference.
5. Compare UI against existing tokens/components and acceptance criteria. Check keyboard, narrow layout and loading/error states; separate taste from correctness.
6. Verify helper claims locally; recheck the fixed failure boundary. Avoid unrelated rewrites. Report coverage/limits when no defect is found.

## Verification and recovery

Support each finding with code or a reproduction. Passing tests narrow uncertainty; they do not prove the app defect-free. Inspect interrupted/unknown effects before replay. Use synthetic evidence and sanitized categories; omit secrets/private paths.

## Result packet

Impact-ordered severity/file:line/trigger/consequence/evidence/correction/regression; concise coverage and limits.

Read references/examples.md with skills_resource_read for examples. Follow coordinator mode and enabled scopes; skills grant no permissions.
