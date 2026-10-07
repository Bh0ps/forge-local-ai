# OpenRouter helpers and independent goal review

In **Settings → OpenRouter**, save and test the connection, explicitly allow
assigned context to leave the computer, then choose **Set up helpers & reviewer**.
This creates read-only Researcher and Assistant profiles, enables delegation,
and enables independent goal review. Edited profiles, their switches, and the
main model/context are preserved. All cloud requests keep the existing free-only
pricing policy, privacy routing and no paid fallback.

## Helpers

The local model sees enabled profile IDs, roles, engines and tool scopes, and
can call `delegate_agent`. The coordinator waits for the helper's result and
returns it to the local model. `agent_result` waits again when needed; approval,
questions, cancellation and paused work are explicitly distinguished from
completion. Helpers inherit their parent's project/workspace and permission
ceiling, share its goal limits and cannot recursively delegate. Writing profiles
still require an isolated Git worktree. Research and Assistant profiles use
OpenRouter without occupying a second local GPU inference slot.

## Goal review

After a top-level goal finishes its checklist, Forge starts a **separate,
read-only OpenRouter run**. Ordinary chat and planning do not trigger goal review.
The reviewer receives the original objective, initial checklist/reviewed plan,
current checklist, final response and a bounded index of actual tool evidence.
It can inspect the connected project and retrieve scoped evidence artifacts.
Private memory, unrelated chats, hidden reasoning, executable tools and recursive
delegation are excluded from the reviewer.

The structured verdict is one of:

- **Complete:** every task has evidence and no fixes remain. Forge marks the goal
  complete after validating the verdict and checking for newer user direction.
- **Needs changes:** specific feedback is saved as a repair task and pinned in
  the local agent's context. The local agent continues, then review runs again.
- **Insufficient evidence:** the goal pauses with the missing evidence explained.
  Resume gathers evidence rather than silently approving an unverified goal.

Invalid JSON, unknown evidence IDs, refusals, truncated output, quota/network
errors and interrupted reviews cannot approve completion. Quota errors do not
automatically retry. **Resume** explicitly retries the saved review or continues
the saved repairs. Review intent, child identity and verdicts persist across
restart; completed tool actions and completed reviewer requests are not replayed.
The default correction limit is three rejections per allowance, configurable
from one to five. Human Resume grants another allowance while retaining history.
Main/child/review usage is recorded separately and included once in shared limits.

Disabling review stops an active review and returns to local completion checks.
This is recorded as **Review disabled**, rather than independent verification.
Remote context, model and privacy settings remain editable. The default review
model is `openrouter/free` with a 32K context budget; availability, supported
features and free request quotas depend on the serving provider.

Model review is an additional quality check, not a proof that arbitrary software
is correct. Review changes and validation evidence before important integration.

Protocol references: [structured outputs](https://openrouter.ai/docs/guides/features/structured-outputs),
[free-model routing](https://openrouter.ai/openrouter/free),
[free request limits](https://openrouter.ai/docs/api/reference/limits), and
[provider routing](https://openrouter.ai/docs/guides/routing/provider-selection).
