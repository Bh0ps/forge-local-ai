---
name: testing
description: Choose meaningful checks for the change and explain what the results prove.
license: MIT
metadata:
  tags: testing, validation, quality
---

# Test & verify

Choose checks from the behavior that changed and the project's existing
commands. Exercise the reported bug or requested outcome, relevant boundaries
and important failure paths. Prefer small representative fixtures and avoid
tests that merely copy the implementation. Keep personal data and account
credentials out of test fixtures.

Run only commands allowed by the current permission profile. Do not send real
messages, publish changes or perform account writes merely to test an adapter.
Report the actual results and distinguish fixture coverage from a live
integration check. Broaden testing when a new failure or concern justifies it.
