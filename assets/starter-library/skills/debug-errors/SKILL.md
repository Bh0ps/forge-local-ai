---
name: debug-errors
description: Reproduce failures, trace their cause and verify the smallest reliable fix.
license: MIT
metadata:
  tags: debugging, errors, reliability
---

# Debug a problem

Capture the concrete symptom, expected behavior and relevant inputs. Inspect
logs and callers with enabled tools, taking care not to repeat a side effect
whose outcome is unknown. Reproduce the failure in a disposable fixture when
possible. Form a hypothesis, verify the cause and fix it at the responsible
boundary. Preserve existing work and avoid unrelated refactors.

Verify the original failure and a meaningful nearby case. Report what changed,
what passed and what remains unverified. Never call a timeout proof that an
external action failed; inspect its actual outcome before proposing a retry.
The user's permission profile continues to apply to every action.
