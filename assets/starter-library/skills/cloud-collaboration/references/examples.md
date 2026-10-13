# Use cloud specialists examples

These examples use synthetic content. Resolve placeholder paths, hashes, IDs, revisions and selectors from current tool results before calling anything. Use only offered tools; examples grant no permissions.

## An advisory review and one clarified linked retry

A listed enabled helper reviews a supplied synthetic form and caller. The local executor continues independent checks while the helper runs. These are sequential lifecycle examples, not three calls to dispatch together.

Call `delegate_agent`:

```json
{
  "agent_id": "<listed enabled review profile>",
  "task": "Read the supplied form component and caller. Review whether delayed replies can overwrite a newer edit, closing the dialog stays closed, and failure leaves a retry. Return concrete trigger/file/line findings and one regression per defect. Do not edit or delegate.",
  "wait": false,
  "required": false
}
```

Call `agent_result`:

```json
{
  "run_id": "<returned direct child id>",
  "wait_seconds": 120
}
```

Call `delegate_agent`:

```json
{
  "agent_id": "<same listed profile>",
  "retry_of": "<failed direct child id with retry_available true>",
  "task": "Review only the supplied form and caller for three behaviors: stale reply rejection, closing during a pending request, and retry after failure. Return evidence or say which evidence is missing. Keep the original scope; do not edit or repeat completed tools.",
  "wait": true,
  "wait_seconds": 120,
  "required": false
}
```

Expected evidence: From delegate_agent retain the direct child ID. From agent_result retain successful/waiting/needs_attention, retry_available, retries_remaining and next_action. Issue the final linked retry only if the failed direct child explicitly has retry_available=true; keep the same logical question and prior completed-tool evidence. Validate significant returned findings locally.

Recovery: A waiting result needs a later bounded read, not another delegation. An explicit refusal or empty_result can permit at most three extra attempts; the coordinator counts them across restart. Never create unrelated tasks to reset that allowance.

## Quota exhaustion on an optional review

The helper returns a quota category and unavailable=true for an advisory assignment. No usable findings were produced.

Expected evidence: State that the optional review is unavailable. Inspect the same component and caller locally and run a disposable failure/retry check. Report those local observations with references, rather than claiming helper success.

Recovery: Quota, authentication, consent and access failures follow their specific next_action. They are not model-refusal retries. Preserve the profile settings and free-route policy; do not invent a paid fallback.

## A required incomplete exchange remains blocked

A required helper cannot continue a provider exchange safely. Its state is needs_attention with a specific protocol/state recovery action, and previous read evidence remains recorded.

Expected evidence: Return the unresolved dependency and next_action with the completed evidence. Do not mark the logical assignment complete merely because an attempt stopped.

Recovery: Keep opaque reasoning, signatures and provider state inside the coordinator. Do not paste it into parent reports, change model aliases, replay completed tools or launch a fresh unlinked assignment to bypass the blocked exchange.
